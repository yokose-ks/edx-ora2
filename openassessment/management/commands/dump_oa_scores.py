from collections import OrderedDict
from datetime import datetime
import json
import logging
from optparse import make_option
import os
from prettytable import PrettyTable
import shutil
import tarfile
import tempfile
import unicodecsv as csv

from boto import connect_s3
from boto.s3.key import Key
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import UTC

from xmodule.modulestore.django import modulestore
from opaque_keys import InvalidKeyError
from opaque_keys.edx.locator import CourseLocator

from openassessment.assessment.models import PeerWorkflow, PeerWorkflowItem
from openassessment.workflow import api as workflow_api
from student.models import user_by_anonymous_id
from submissions.models import Score, Submission


log = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    This command allows you to dump all openassessment submissions with some related data.

    Usage: python manage.py lms --settings=aws dump_oa_scores edX/DemoX/Demo_Course

    Args:
        course_id (unicode): The ID of the course to the openassessment item exists in,
                             like 'org/course/run' or 'course-v1:org+course+run'
    """
    help = """Usage: dump_oa_scores [-d /tmp/dump_oa_scores] [-w] <course_id>"""
    option_list = BaseCommand.option_list + (
        make_option('-d', '--dump-dir',
                    action="store",
                    dest='dump_dir',
                    default=None,
                    help='Directory in which csv file is to be dumped'),
        make_option('-w', '--with-attachments',
                    action="store_true",
                    dest='with_attachments',
                    default=False,
                    help='Whether to gather submission attachments'),
    )

    def handle(self, *args, **options):
        if len(args) != 1:
            raise CommandError("This command requires only one argument: <course_id>")

        course_id, = args
        # Check args: course_id
        try:
            course_id = CourseLocator.from_string(course_id)
        except InvalidKeyError:
            raise CommandError("The course_id is not of the right format. It should be like 'org/course/run' or 'course-v1:org+course+run'")
        if not modulestore().get_course(course_id):
            raise CommandError("No such course was found.")

        # S3 store
        try:
            conn = connect_s3(settings.AWS_ACCESS_KEY_ID, settings.AWS_SECRET_ACCESS_KEY)
            bucket = conn.get_bucket(settings.FILE_UPLOAD_STORAGE_BUCKET_NAME)
        except Exception as e:
            print e
            raise CommandError("Could not establish a connection to S3 for file upload. Check your credentials.")

        # Dump directory
        dump_dir = options['dump_dir'] or '/tmp/dump_oa_scores'
        if not os.path.exists(dump_dir):
            os.makedirs(dump_dir)

        # Whether to gather attachments
        with_attachments = options['with_attachments']

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
                selected_oa_item = int(selected)
                oa_item = oa_items[selected_oa_item]
                break
            except (IndexError, ValueError):
                print "WARN: Invalid number was detected. Choose again."
                continue

        item_location = oa_item.location

        # Get submissions from course_id and item_location
        submissions = get_submissions(course_id, item_location)

        header = ['Title', 'User name', 'Submission content', 'Submission created at', 'Status', 'Points earned', 'Points possible', 'Score created at', 'Grade count', 'Being graded count', 'Scored count']
        header_extra = []
        rows = []

        for submission in submissions:
            row = []
            # 'Title'
            row.append(oa_item.title)
            print 'submission_uuid=%s' % submission.submission_uuid
            # 'User Name'
            user = user_by_anonymous_id(submission.student_id)
            row.append(user.username)
            # 'Submission Content'
            raw_answer = Submission.objects.get(uuid=submission.submission_uuid).raw_answer
            row.append(json.loads(raw_answer)['text'].replace('\n', '[\\n]'))
            # 'Submission created at'
            row.append(submission.created_at)
            # 'Status'
            row.append(get_workflow_info(submission.submission_uuid, oa_item)['status'])

            # 'Points earned', 'Points possible', 'Score created at'
            latest_score = get_latest_score(submission)
            if latest_score is not None:
                row.append(latest_score.points_earned)
                row.append(latest_score.points_possible)
                row.append(latest_score.created_at)
            else:
                row.append('')
                row.append('')
                row.append('')

            # 'Grade count'
            row.append(submission.num_peers_graded())
            # 'Being graded count'
            row.append(len(PeerWorkflowItem.objects.filter(author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False)))
            # 'Scored count'
            row.append(len(PeerWorkflowItem.objects.filter(author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False, scored=True)))

            # assessments from others
            assessed_items = PeerWorkflowItem.objects.filter(author=submission.id, submission_uuid=submission.submission_uuid, assessment__isnull=False).order_by('assessment')
            if assessed_items:
                # 'Scored flags'
                header_extra.append('Scored flags')
                row.append(','.join([str(int(assessed_item.scored)) for assessed_item in assessed_items]))
                # 'Scorer Usernames'
                header_extra.append('Scorer usernames')
                scorer_usernames = [user_by_anonymous_id(assessed_item.scorer.student_id).username for assessed_item in assessed_items]
                row.append(','.join(scorer_usernames))

                assessed_assessments = [assessed_item.assessment for assessed_item in assessed_items]
                scores = scores_by_criterion(assessed_assessments)
                # 'Rubric points'
                for i, score in enumerate(scores.items()):
                    header_extra.append('Rubric(%s) points' % score[0])
                    row.append(','.join(map(str, score[1])))
            else:
                pass

            rows.append(row)

        # Create csv file
        header.extend(sorted(set(header_extra), key=header_extra.index))
        csv_filename = 'oa_scores-%s-#%d.csv' % (course_id.to_deprecated_string().replace('/', '.'), selected_oa_item)
        csv_filepath = os.path.join(dump_dir, csv_filename)
        write_csv(csv_filepath, header, rows)
        # Upload to S3
        upload_file_to_s3(bucket, csv_filename, csv_filepath)

        # Download images from S3
        if with_attachments:
            temp_dir = tempfile.mkdtemp()
            for submission in submissions:
                file_key = u"{prefix}/{student_id}/{course_id}/{item_id}".format(
                    prefix=settings.FILE_UPLOAD_STORAGE_PREFIX,
                    student_id=submission.student_id,
                    course_id=course_id.to_deprecated_string(),
                    item_id=oa_item.location.to_deprecated_string(),
                )
                try:
                    key = bucket.get_key(file_key)
                except:
                    print "WARN: No such file in S3 [%s]" % file_key
                    continue
                user = user_by_anonymous_id(submission.student_id)
                user_path = os.path.join(temp_dir, user.username)
                try:
                    key.get_contents_to_filename(user_path)
                except:
                    print "WARN: Could not download file from S3 [%s]" % file_key
                    continue
            # Compress and upload to S3
            tar_filename = 'oa_scores-%s-#%d.tar.gz' % (course_id.to_deprecated_string().replace('/', '.'), selected_oa_item)
            tar_filepath = os.path.join(dump_dir, tar_filename)
            tar = tarfile.open(tar_filepath, 'w:gz')
            tar.add(temp_dir, arcname=tar_filename)
            shutil.rmtree(temp_dir)
            upload_file_to_s3(bucket, tar_filename, tar_filepath)


def write_csv(filepath, header, rows):
    try:
        with open(filepath, 'wb') as output_file:
            writer = csv.writer(output_file)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
    except IOError:
        raise CommandError("Error writing to file: %s" % filepath)
    print "Successfully created csv file: %s" % filepath


def upload_file_to_s3(bucket, filename, filepath):
    try:
        s3key = Key(bucket)
        s3key.key = "dump_oa_scores/{0}".format(filename)
        s3key.set_contents_from_filename(filepath)
    except:
        raise
    finally:
        s3key.close()
    print "Successfully uplaoded file to S3: %s/%s" % (bucket.name, s3key.key)


def get_submissions(course_id, item_location):
    submissions = PeerWorkflow.objects.filter(course_id=course_id, item_id=item_location)
    if not submissions:
        raise CommandError("No submission was found.")
    return submissions


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


# Note: modified from apps/openassessment/assessment/models/base.py Assessment.scores_by_criterion()
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
