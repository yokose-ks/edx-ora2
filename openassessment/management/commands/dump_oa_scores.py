from collections import OrderedDict
import json
import logging
from itertools import izip_longest
from unicodedata import east_asian_width
import unicodecsv as csv

from django.core.management.base import BaseCommand, CommandError

from xmodule.modulestore import Location
from xmodule.modulestore.django import modulestore

from openassessment.assessment.models import PeerWorkflow, PeerWorkflowItem
from openassessment.workflow import api as workflow_api
from student.models import user_by_anonymous_id
from submissions.models import Score, Submission


log = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    This command allows you to dump all openassessment submissions with some related data.

    Usage: python manage.py cms dump_oa_scores --settings=aws edX/Open_DemoX/edx_demo_course

    Args:
        course_id (unicode): The ID of the course to the openassessment item exists in, e.g. edX/Open_DemoX/edx_demo_course
    """
    help = """Usage: dump_oa_scores <course_id>"""

    def handle(self, *args, **options):
        if len(args) != 1:
            raise CommandError("This command requires only one argument: <course_id>")

        course_id, = args
        # Check args: course_id
        try:
            Location.parse_course_id(course_id)
        except ValueError:
            raise CommandError("The course_id is not of the right format. It should be like 'org/course/name'")

        # Find course
        course_dict = Location.parse_course_id(course_id)
        tag = 'i4x'
        org = course_dict['org']
        course = course_dict['course']
        name = course_dict['name']
        course_items = modulestore().get_items(Location(tag, org, course, 'course', name))
        if not course_items:
            raise CommandError("No such course was found.")

        # Find openassessment items
        oa_items = modulestore().get_items(Location(tag, org, course, 'openassessment'))
        if not oa_items:
            raise CommandError("No openassessment item was found.")
        oa_items = sorted(oa_items, key=lambda item:item.start)
        print "Openassessment item(s):"
        oa_output = SimpleTable()
        oa_output.set_header(['#', 'Item ID', 'Title'])
        for i, oa_item in enumerate(oa_items):
            row = []
            row.append(i)
            row.append(oa_item.id)
            row.append(oa_item.title)
            oa_output.add_row(row)
        oa_output.print_table()
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

        item_id = oa_item.id

        # Get submissions from course_id and item_id
        submissions = get_submissions(course_id, item_id)

        header = ['Title', 'User name', 'Submission content', 'Submission created at', 'Status', 'Points earned', 'Points possible', 'Score created at', 'Grade count', 'Being graded count', 'Scored count']
        header_extra = []
        rows = []

        for submission in submissions:
            row = []
            # 'Title'
            row.append(oa_item.title)
            print('@@@ submission_uuid=%s' % submission.submission_uuid)
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

        header.extend(sorted(set(header_extra), key=header_extra.index))
        write_csv('oa_scores-%s-#%d.csv' % (course_id.replace('/', '.'), selected_oa_item), header, rows)


def write_csv(output_filename, header, rows):
    try:
        with open(output_filename, 'wb') as output_file:
            writer = csv.writer(output_file)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
    except IOError:
        raise CommandError("Error writing to file: %s" % output_filename)


def get_submissions(course_id, item_id):
    submissions = PeerWorkflow.objects.filter(course_id=course_id, item_id=item_id)
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


class SimpleTable(object):
    """
    SimpleTable
    """
    def __init__(self, header=None, rows=None):
        self.header = header or ()
        self.rows = rows or []

    def set_header(self, header):
        self.header = header

    def add_row(self, row):
        self.rows.append(row)

    def _calc_maxes(self):
        array = [self.header] + self.rows
        return [max(self._unicode_width(s) for s in ss) for ss in izip_longest(*array, fillvalue='')]

    def _unicode_width(self, s, width={'F': 2, 'H': 1, 'W': 2, 'Na': 1, 'A': 2, 'N': 1}):
        s = unicode(s)
        return sum(width[east_asian_width(c)] for c in s)

    def _get_printable_row(self, row):
        maxes = self._calc_maxes()
        return '| ' + ' | '.join([unicode(r) + ' ' * (m - self._unicode_width(r)) for r, m in izip_longest(row, maxes, fillvalue='')]) + ' |'

    def _get_printable_header(self):
        return self._get_printable_row(self.header)

    def _get_printable_border(self):
        maxes = self._calc_maxes()
        return '+-' + '-+-'.join(['-' * m for m in maxes]) + '-+'

    def get_table(self):
        lines = []
        if self.header:
            lines.append(self._get_printable_border())
            lines.append(self._get_printable_header())
        lines.append(self._get_printable_border())
        for row in self.rows:
            lines.append(self._get_printable_row(row))
        lines.append(self._get_printable_border())
        return lines

    def print_table(self):
        lines = self.get_table()
        for line in lines:
            print(line)
