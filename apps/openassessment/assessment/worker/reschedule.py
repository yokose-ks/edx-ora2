"""
Asynchronous tasks for grading essays using text classifiers.
"""

from celery import task
from celery.utils.log import get_task_logger
from openassessment.assessment.api import ai_worker as ai_worker_api
from openassessment.assessment.errors import AIError
from .algorithm import AIAlgorithm, AIAlgorithmError
from openassessment.assessment.errors.ai import AITrainingInternalError, AIGradingInternalError
from openassessment.assessment.models.ai import AIGradingWorkflow, AIClassifierSet, AITrainingWorkflow
from openassessment.assessment.worker import grading as grading_tasks
from openassessment.assessment.worker import training as training_tasks

MAX_RETRIES = 1

logger = get_task_logger(__name__)

@task(max_retries=MAX_RETRIES)  # pylint: disable=E1102
def reschedule_grading_tasks(course_id, item_id):

    training_workflows = AITrainingWorkflow.get_incomplete_workflows(course_id, item_id)
    grading_workflows = AIGradingWorkflow.get_incomplete_workflows(course_id, item_id)

    # Only reschedules the Grading Tasks if there were not incomplete training workflows.
    # If we choose to remove this step, we will likely see a ton of Failures within this
    # section, as a Grading Task will automatically fail if the classifiers are not complete.

    training_workflows_exist = False
    for tw in training_workflows:
        training_workflows_exist = True

    if not training_workflows_exist:

        # Try to grade all incomplete grading workflows
        for workflow in grading_workflows:

            # Check to see if we have already defined classifiers for this example or not
            # Try to define them if they have not been defined

            if workflow.classifier_set is None:
                try:
                    classifier_set_candidates = AIClassifierSet.objects.filter(
                        rubric=workflow.rubric, algorithm_id=workflow.algorithm_id
                    ).order_by('-created_at')[:1]
                    workflow.classifier_set = classifier_set_candidates[0]
                    workflow.save()
                except Exception as ex:
                    msg = u"An error occured while retrying to assign classifiers to an assignment: {}".format(ex)
                    logger.exception(msg)
                    raise reschedule_grading_tasks.retry()

            # Now we should (unless we had an exception above) have a classifier set.
            try:
                grading_tasks.grade_essay.apply_async(args=[workflow.uuid])
            except Exception as ex:
                msg = u"An error occurred while try to grade this essay: {}".format(ex)
                logger.exception(msg)
                raise reschedule_grading_tasks.retry()


@task(max_retries=MAX_RETRIES) #pylint: disable E=1102
def reschedule_training_tasks(course_id, item_id):

    # Run a query to find the incomplete training workflows
    training_workflows = AITrainingWorkflow.get_incomplete_workflows(course_id, item_id)

    # Tries to train every workflow that has not completed.
    for target_workflow in training_workflows:
        try:
            training_tasks.train_classifiers.apply_async(args=[target_workflow.uuid])
        except Exception as ex:
            msg = (
                u"An unexpected error occurred while scheduling the task for training workflow with UUID {}"
            ).format(target_workflow.uuid)
            logger.exception(msg)
            raise reschedule_training_tasks.retry()