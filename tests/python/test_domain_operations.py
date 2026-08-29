import unittest

from clipen.domain import (
    Attempt,
    AttemptState,
    InvalidTransition,
    Operation,
    OperationKind,
    OperationState,
)


class OperationStateTests(unittest.TestCase):
    def operation(self) -> Operation:
        return Operation("operation-1", "project-1", OperationKind.PLAN_GENERATION, "snapshot-1")

    def test_happy_path_is_immutable_and_versioned(self) -> None:
        queued = self.operation()
        running = queued.transition(OperationState.RUNNING)
        succeeded = running.transition(OperationState.SUCCEEDED)
        self.assertEqual(queued.state, OperationState.QUEUED)
        self.assertEqual((running.revision, succeeded.revision), (1, 2))
        self.assertEqual(succeeded.state, OperationState.SUCCEEDED)

    def test_failed_operation_can_be_requeued_but_attempt_cannot(self) -> None:
        failed = self.operation().transition(OperationState.RUNNING).transition(OperationState.FAILED)
        self.assertEqual(failed.transition(OperationState.QUEUED).revision, 3)

        attempt = Attempt("attempt-1", failed.operation_id, 1)
        attempt = attempt.transition(AttemptState.RUNNING).transition(AttemptState.FAILED)
        with self.assertRaises(InvalidTransition):
            attempt.transition(AttemptState.QUEUED)

    def test_cancel_request_can_race_with_success(self) -> None:
        operation = self.operation().transition(OperationState.RUNNING)
        operation = operation.transition(OperationState.CANCEL_REQUESTED)
        operation = operation.transition(OperationState.SUCCEEDED)
        self.assertEqual(operation.state, OperationState.SUCCEEDED)

    def test_terminal_states_and_invalid_identifiers_fail_closed(self) -> None:
        completed = self.operation().transition(OperationState.RUNNING).transition(OperationState.SUCCEEDED)
        with self.assertRaises(InvalidTransition):
            completed.transition(OperationState.QUEUED)
        with self.assertRaises(ValueError):
            Operation("", "project-1", OperationKind.EDIT_EXECUTION, "snapshot-1")


if __name__ == "__main__":
    unittest.main()
