import unittest

from clipen.domain import InvalidTransition, PlanRevision, PlanStatus


class PlanRevisionTests(unittest.TestCase):
    def plan(self) -> PlanRevision:
        return PlanRevision(
            plan_id="plan-1",
            project_id="project-1",
            revision=1,
            content_hash="a" * 64,
            material_snapshot_id="materials-1",
            analysis_snapshot_id="analysis-1",
            system_skill_release="skills-1",
        )

    def test_confirmed_revision_is_the_only_executable_state(self) -> None:
        ready = self.plan().transition(PlanStatus.READY_FOR_REVIEW)
        confirmed = ready.transition(PlanStatus.CONFIRMED)
        self.assertFalse(ready.can_execute)
        self.assertTrue(confirmed.can_execute)
        self.assertEqual(self.plan().status, PlanStatus.DRAFT)

    def test_accepted_revision_is_terminal(self) -> None:
        plan = self.plan()
        for status in (
            PlanStatus.READY_FOR_REVIEW,
            PlanStatus.CONFIRMED,
            PlanStatus.EXECUTING,
            PlanStatus.REVIEWED,
            PlanStatus.ACCEPTED,
        ):
            plan = plan.transition(status)
        with self.assertRaises(InvalidTransition):
            plan.transition(PlanStatus.SUPERSEDED)

    def test_hash_and_revision_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            PlanRevision("plan-1", "project-1", 0, "a" * 64, "materials-1", "analysis-1", "skills-1")
        with self.assertRaises(ValueError):
            PlanRevision("plan-1", "project-1", 1, "not-a-hash", "materials-1", "analysis-1", "skills-1")


if __name__ == "__main__":
    unittest.main()
