from collections import OrderedDict
from datetime import datetime
import logging
from prettytable import PrettyTable

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import now, UTC

from xmodule.modulestore.django import modulestore
from opaque_keys import InvalidKeyError
from opaque_keys.edx.locator import CourseLocator

from openassessment.assessment.api import peer as peer_api
from openassessment.assessment.models import Assessment, PeerWorkflow, PeerWorkflowItem
from openassessment.workflow import api as workflow_api
from student.models import anonymous_id_for_user
from submissions import api as sub_api
from submissions.models import Score


log = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    This command allows you to rescore the user's openassessment by switching peer-assessments.
    In addition, it also allows you to create a new peer-assessment record and attach it to the specified users (like staff).

    Usage: python manage.py cms --settings=aws rescore_oa edX/DemoX/Demo_Course

    Args:
        course_id (unicode): The ID of the course to the openassessment item exists in,
                             like 'org/course/run' or 'course-v1:org+course+run'
        username (unicode): The author's username of the submission to be treated
    """
    help = """Usage: rescore_oa <course_id> <username>"""

    def handle(self, *args, **options):
        if len(args) != 2:
            raise CommandError("This command requires two arguments: <course_id> <username>")

        course_id, username, = args
        # Check args: course_id
        try:
            course_id = CourseLocator.from_string(course_id)
        except InvalidKeyError:
            raise CommandError("The course_id is not of the right format. It should be like 'org/course/run' or 'course-v1:org+course+run'")

        # Find course
        course_items = modulestore().get_items(course_id, qualifiers={'category': 'course'})
        if not course_items:
            raise CommandError("No such course was found.")

        # Find openassessment items
        oa_items = modulestore().get_items(course_id, qualifiers={'category': 'openassessment'})
        if not oa_items:
            raise CommandError("No openassessment item was found.")
        oa_items = sorted(oa_items, key=lambda item:item.start or datetime(2030, 1, 1, tzinfo=UTC()))
        print "Openassessment item(s):"
        oa_output = PrettyTable(['#', 'Item ID', 'Title'])
        oa_output.align = 'l'
        for i, oa_item in enumerate(oa_items):
            row = []
            row.append(i)
            row.append(oa_item.location)
            row.append(oa_item.title)
            oa_output.add_row(row)
        print oa_output
        while True:
            try:
                selected = raw_input("Choose an openassessment item # (empty to cancel): ")
                if selected == '':
                    print "Cancelled."
                    return
                selected = int(selected)
                oa_item = oa_items[selected]
                break
            except (IndexError, ValueError):
                print "WARN: Invalid number was detected. Choose again."
                continue

        item_location = oa_item.location

        # Get student_id from username
        # TODO: courseenrollment parameters can be used by only lms?
        students = User.objects.filter(username=username, is_active=True, courseenrollment__course_id=course_id, courseenrollment__is_active=True)
        if not students:
            raise CommandError("No such user was found.")
        student = students[0]
        anonymous_student_id = anonymous_id_for_user(student, course_id)

        # Get submission from student_id, course_id and item_location
        submission = get_submission(course_id, item_location, anonymous_student_id)

        # Print summary
        print_summary(course_id, oa_item, anonymous_student_id)

        while True:
            print "[0] Show the user's submission again."
            print "[1] Toggle the `scored` flag in the peer-assessment record."
            print "[2] Create a new peer-assessment record to the users."
            resp = raw_input("Choose an operation (empty to cancel): ")

            if resp == '0':
                print_summary(course_id, oa_item, anonymous_student_id)

            elif resp == '1':
                while True:
                    try:
                        selected_item_id = raw_input("Please input PeerWorkflowItem ID to toggle the `scored` flag (empty to cancel): ")
                        if selected_item_id == '':
                            print "Cancelled."
                            break
                        selected_item_id = int(selected_item_id)
                        selected_item = PeerWorkflowItem.objects.filter(id=selected_item_id, author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False)[0]
                    except (IndexError, ValueError):
                        print "WARN: Invalid ID was detected. Input again."
                        continue
                    # Update PeerWorkflowItem (assessment_peerworkflowitem record)
                    selected_item.scored = not selected_item.scored
                    selected_item.save()
                    # Update Score (submissions_score record)
                    latest_score = get_latest_score(submission)
                    if latest_score is not None:
                        max_scores = peer_api.get_rubric_max_scores(submission.submission_uuid)
                        try:
                            median_scores = peer_api.get_assessment_median_scores(submission.submission_uuid)
                        except:
                            median_scores = {}
                        sub_api.set_score(submission.submission_uuid, sum(median_scores.values()), sum(max_scores.values()))
                        #latest_score.points_earned = sum(median_scores.values())
                        #latest_score.created_at = now()
                        #latest_score.save()
                    # Update status of AssessmentWorkflow (workflow_assessmentworkflow record)
                    get_workflow_info(submission.submission_uuid, oa_item)

                    # Print summary
                    print_summary(course_id, oa_item, anonymous_student_id)

            elif resp == '2':
                while True:
                    staff_username = raw_input("Please input username to be given a new peer-assessment item (empty to cancel): ")
                    if staff_username == '':
                        print "Cancelled."
                        break
                    # TODO: courseenrollment parameters can be used by only lms?
                    staffs = User.objects.filter(username=staff_username, is_active=True, courseenrollment__course_id=course_id, courseenrollment__is_active=True)
                    if not staffs:
                        print "WARN: No such user was found in the course. Input again."
                        continue
                    staff = staffs[0]
                    anonymous_staff_id = anonymous_id_for_user(staff, course_id)
                    staff_submissions = PeerWorkflow.objects.filter(course_id=course_id, item_id=item_location, student_id=anonymous_staff_id)
                    if not staff_submissions:
                        print "WARN: This user hasn't posted any submission in this openassessment item yet. Input again."
                        continue
                    staff_submission = staff_submissions[0]
                    # Check if this user has already assessed the requested submission
                    items_assessed_by_staff = PeerWorkflowItem.objects.filter(
                        scorer=staff_submission,
                        author=submission,
                        submission_uuid=submission.submission_uuid
                    )
                    if len(items_assessed_by_staff) > 0:
                        print "WARN: This user has already assessed the requested submission. Input again."
                        continue
                    print "Staff submission:"
                    print_submission(staff_submission, oa_item)

                    while True:
                        resp = raw_input("Is this right? (y/n): ")
                        if resp.lower() == 'y':
                            new_items = PeerWorkflowItem.objects.filter(scorer_id=staff_submission.id, assessment__isnull=True).order_by('-started_at')
                            if new_items:
                                # Replace the author and submission_uuid
                                new_item = new_items[0]
                                new_item.author = submission
                                new_item.submission_uuid = submission.submission_uuid
                                new_item.started_at = now()
                            else:
                                new_item = PeerWorkflowItem.objects.create(
                                    scorer=staff_submission,
                                    author=submission,
                                    submission_uuid=submission.submission_uuid,
                                    started_at=now()
                                )
                            new_item.save()
                            print "Create a new peer-assessment record to %s successfully!" % staff.username
                            break
                        elif resp.lower() == 'n':
                            break
                        else:
                            continue

            elif resp == '':
                print "Cancelled."
                break
            else:
                print "WARN: Invalid number was detected. Choose again."
                continue


def get_submission(course_id, item_location, anonymous_student_id):
    submissions = PeerWorkflow.objects.filter(course_id=course_id, item_id=item_location, student_id=anonymous_student_id)
    if not submissions:
        raise CommandError("No submission was found.")
    if len(submissions) > 1:
        raise CommandError("Duplicate submissions were found.")
    submission = submissions[0]
    return submission


def get_latest_score(submission):
    try:
        latest_score = Score.objects.filter(
            submission__uuid=submission.submission_uuid
        ).order_by("-id").select_related("submission")[0]
        if latest_score.is_hidden():
            latest_score = None
    except IndexError:
        latest_score = None

    return latest_score


# Note: modified from openassessment/xblock/workflow_mixin.py WorkflowMixin.get_workflow_info()
def get_workflow_info(submission_uuid, oa_item):
    assessments = oa_item.rubric_assessments
    requirements = {}

    peer_assessment_module = get_assessment_module(assessments, 'peer-assessment')
    if peer_assessment_module:
        requirements["peer"] = {
            "must_grade": peer_assessment_module["must_grade"],
            "must_be_graded_by": peer_assessment_module["must_be_graded_by"]
        }

    training_module = get_assessment_module(assessments, 'student-training')
    if training_module:
        requirements["training"] = {
            "num_required": len(training_module["examples"])
        }

    # Note that this *may* update the workflow status if it's changed.
    # If the status is already `done`, the status stands immobile.
    return workflow_api.get_workflow_for_submission(submission_uuid, requirements)


# Note: modified from openassessment/xblock/openassessmentblock.py OpenAssessmentBlock.get_assessment_module()
def get_assessment_module(assessments, mixin_name):
    for assessment in assessments:
        if assessment["name"] == mixin_name:
            return assessment


# Note: modified from openassessment/assessment/models/base.py Assessment.scores_by_criterion()
def scores_by_criterion(assessments):
    """Create a dictionary of lists for scores associated with criterion

    Create a key value in a dict with a list of values, for every criterion
    found in an assessment.

    Iterate over every part of every assessment. Each part is associated with
    a criterion name, which becomes a key in the score dictionary, with a list
    of scores.

    Args:
        assessments (list): List of assessments to sort scores by their
            associated criteria.

    Examples:
        >>> assessments = Assessment.objects.all()
        >>> scores_by_criterion(assessments)
        {
            "foo": [1, 2, 3],
            "bar": [6, 7, 8]
        }
    """
    assessments = list(assessments)  # Force us to read it all
    if not assessments:
        return []

    scores = OrderedDict()

    for assessment in assessments:
        for part in assessment.parts.all().select_related("option__criterion").order_by('option__criterion__order_num'):
            criterion_name = part.option.criterion.name
            # Note: modified because non-ascii key makes defaultdict out of order.
            if criterion_name not in scores:
                scores[criterion_name] = [part.option.points]
            else:
                scores[criterion_name].append(part.option.points)

    return scores


def print_summary(course_id, oa_item, anonymous_student_id):
    # Print submission
    submission = get_submission(course_id, oa_item.location, anonymous_student_id)
    print "Submission status:"
    print_submission(submission, oa_item)

    # Print scored assessment(s)
    scored_items = PeerWorkflowItem.objects.filter(author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False, scored=True).order_by('assessment')
    print "Scored assessment(s):"
    if scored_items:
        scored_assessments = [scored_item.assessment for scored_item in scored_items]
        scored_scores = scores_by_criterion(scored_assessments)
        median_score_dict = Assessment.get_median_score_dict(scored_scores)
        print_peerworkflowitem(scored_items, scored_scores)
    else:
        scored_scores = {}
        print "... No record was found."

    # Print not-scored assessment(s)
    not_scored_items = PeerWorkflowItem.objects.filter(author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False, scored=False).order_by('assessment')
    print "Not-scored assessment(s):"
    if not_scored_items:
        not_scored_assessments = [not_scored_item.assessment for not_scored_item in not_scored_items]
        not_scored_scores = scores_by_criterion(not_scored_assessments)
        print_peerworkflowitem(not_scored_items, not_scored_scores)
    else:
        print "... No record was found."

    # Print latest score
    latest_score = get_latest_score(submission)
    print "Latest score:"
    if latest_score is not None:
        try:
            median_scores = peer_api.get_assessment_median_scores(submission.submission_uuid)
        except:
            median_scores = {}
        latest_score_output = PrettyTable(['Score ID'] + scored_scores.keys() + ['Points earned', 'Points possible', 'Created at'])
        latest_score_output.align = 'l'
        row = []
        row.append(latest_score.id)
        row.extend([median_scores[k] for k in scored_scores.keys()])
        row.append(latest_score.points_earned)
        row.append(latest_score.points_possible)
        row.append(latest_score.created_at)
        latest_score_output.add_row(row)
        print latest_score_output
    else:
        print "... No record was found."


def print_submission(submission, oa_item):
    submission_output = PrettyTable(['Submission UUID', 'Status', 'Grade count', 'Being graded count', 'Created at', 'Must-grade completed at', 'Must-be-graded completed at'])
    submission_output.align = 'l'
    row = []
    row.append(submission.submission_uuid)
    row.append(get_workflow_info(submission.submission_uuid, oa_item)['status'])
    row.append(submission.num_peers_graded())
    row.append(len(PeerWorkflowItem.objects.filter(author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False)))
    row.append(submission.created_at)
    row.append(submission.completed_at)
    row.append(submission.grading_completed_at)
    submission_output.add_row(row)
    print submission_output


def print_peerworkflowitem(items, scores):
    output = PrettyTable(['PeerWorkflowItem ID', 'Scored'] + scores.keys() + ['Feedback'])
    output.align = 'l'
    for i, item in enumerate(items):
        row = []
        row.append(item.id)
        row.append(item.scored)
        row.extend([v[i] for v in scores.values()])
        # Omit the excessive characters
        row.append(item.assessment.feedback.replace('\n', '')[:30])
        output.add_row(row)
    print output
