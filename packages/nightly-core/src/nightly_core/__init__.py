"""Nightly core — the loop, priority cascade, drain, and briefing renderer.

This package is host-agnostic. Per-host integrations (nightly-host-claude,
nightly-host-codex, ...) implement the `NightlyHostIntegration` contract and
the shared core calls into them.
"""

from nightly_core._version import __version__
from nightly_core.autonomy import (
    AUTO_PR_CATEGORIES,
    AUTO_PR_LOC_CEILING,
    auto_pr_rejection_reason,
    can_auto_pr,
)
from nightly_core.briefing import (
    BriefingContext,
    build_context,
    render_briefing,
    write_briefing,
)
from nightly_core.cascade import (
    CASCADE_SOURCES,
    CascadeChoice,
    CascadeSource,
    pick_accepted_rfc,
    pick_github_issue,
    pick_ideated,
    pick_in_flight,
    pick_unblocked,
)
from nightly_core.cascade import (
    next_task as cascade_next,
)
from nightly_core.conclude_skill import CONCLUDE_SKILL_MD, UPDATE_SKILL_MD
from nightly_core.contract import (
    AuthStatus,
    HostId,
    InstallScope,
    KeepaliveSupport,
    NightlyHostIntegration,
    SpecialistRole,
    SubAgentResult,
)
from nightly_core.doctor import DoctorCheck, DoctorReport, diagnose_and_repair
from nightly_core.driver import (
    DriverConfig,
    TaskOutcome,
    build_task_prompt,
    run_loop,
    run_one_task,
)
from nightly_core.headless import (
    HeadlessResult,
    SubprocessRunner,
    default_subprocess_runner,
    run_subprocess,
)
from nightly_core.ideation import run_proposers, top_auto_pr, write_drafts
from nightly_core.paths import (
    current_run_pointer,
    new_run_id,
    nightly_dir,
    planning_dir,
    repo_root,
    run_dir,
    runs_dir,
)
from nightly_core.plans import (
    PLAN_STATUSES,
    PlanRecord,
    PlanStatus,
    list_plans,
    read_plan,
    update_plan_status,
)
from nightly_core.proposers import (
    LintDebtProposer,
    Proposal,
    Proposer,
    ProposerCategory,
    TodoFixmeProposer,
    TypeHoleProposer,
    default_proposers,
)
from nightly_core.runs import (
    Run,
    TaskDir,
    conclude_run,
    current_run,
    list_runs,
    new_task,
    next_task_index,
    slugify,
    start_run,
)
from nightly_core.specialists import all_roles, specialist_prompt
from nightly_core.triage import (
    IssueFetcher,
    IssueRanking,
    IssueRecord,
    fetch_via_gh,
    rank_issues,
    score_issue,
)

__all__ = [
    "AUTO_PR_CATEGORIES",
    "AUTO_PR_LOC_CEILING",
    "CASCADE_SOURCES",
    "CONCLUDE_SKILL_MD",
    "PLAN_STATUSES",
    "UPDATE_SKILL_MD",
    "AuthStatus",
    "BriefingContext",
    "CascadeChoice",
    "CascadeSource",
    "DoctorCheck",
    "DoctorReport",
    "DriverConfig",
    "HeadlessResult",
    "HostId",
    "InstallScope",
    "IssueFetcher",
    "IssueRanking",
    "IssueRecord",
    "KeepaliveSupport",
    "LintDebtProposer",
    "NightlyHostIntegration",
    "PlanRecord",
    "PlanStatus",
    "Proposal",
    "Proposer",
    "ProposerCategory",
    "Run",
    "SpecialistRole",
    "SubAgentResult",
    "SubprocessRunner",
    "TaskDir",
    "TaskOutcome",
    "TodoFixmeProposer",
    "TypeHoleProposer",
    "__version__",
    "all_roles",
    "auto_pr_rejection_reason",
    "build_context",
    "build_task_prompt",
    "can_auto_pr",
    "cascade_next",
    "conclude_run",
    "current_run",
    "current_run_pointer",
    "default_proposers",
    "default_subprocess_runner",
    "diagnose_and_repair",
    "fetch_via_gh",
    "list_plans",
    "list_runs",
    "new_run_id",
    "new_task",
    "next_task_index",
    "nightly_dir",
    "pick_accepted_rfc",
    "pick_github_issue",
    "pick_ideated",
    "pick_in_flight",
    "pick_unblocked",
    "planning_dir",
    "rank_issues",
    "read_plan",
    "render_briefing",
    "repo_root",
    "run_dir",
    "run_loop",
    "run_one_task",
    "run_proposers",
    "run_subprocess",
    "runs_dir",
    "score_issue",
    "slugify",
    "specialist_prompt",
    "start_run",
    "top_auto_pr",
    "update_plan_status",
    "write_briefing",
    "write_drafts",
]
