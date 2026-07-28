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
from nightly_core.bug import DEFAULT_BUG_REPO, BugReport
from nightly_core.bug import build_report as build_bug_report
from nightly_core.bug import gh_command as bug_gh_command
from nightly_core.bug import submit_report as submit_bug_report
from nightly_core.bug import write_report as write_bug_report
from nightly_core.cascade import (
    CASCADE_SOURCES,
    CascadeChoice,
    CascadeSource,
    count_open_nightly_prs,
    pick_accepted_rfc,
    pick_github_issue,
    pick_ideated,
    pick_ideated_fallback,
    pick_in_flight,
    pick_unblocked,
)
from nightly_core.cascade import (
    next_task as cascade_next,
)
from nightly_core.ci_watch import (
    CICheck,
    PRCIStatus,
    fetch_pr_checks,
    list_ci_status,
)
from nightly_core.conclude_skill import (
    BUG_SKILL_MD,
    CONCLUDE_SKILL_MD,
    INIT_SKILL_MD,
    UPDATE_SKILL_MD,
)
from nightly_core.config import (
    ContextConfig,
    ModelTierConfig,
    ParallelismConfig,
    TierBinding,
    load_context_config,
    load_model_tier_config,
    load_parallelism_config,
)
from nightly_core.contract import (
    MODEL_TIERS,
    AuthStatus,
    HostId,
    InstallScope,
    KeepaliveSupport,
    ModelTier,
    NightlyHostIntegration,
    ReasoningEffort,
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
    MODEL_TIER_KEY,
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
from nightly_core.routing import (
    ContextThresholds,
    ResolvedDispatch,
    resolve_context_thresholds,
    resolve_model_for_task,
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
from nightly_core.specialists import (
    SPECIALIST_TIER_DEFAULTS,
    all_roles,
    specialist_prompt,
    tier_for_role,
)
from nightly_core.triage import (
    IssueFetcher,
    IssueRanking,
    IssueRecord,
    fetch_via_gh,
    rank_issues,
    score_issue,
)
from nightly_core.verify import VerifyCheck, VerifyReport, detect_checks, run_verify

__all__ = [
    "AUTO_PR_CATEGORIES",
    "AUTO_PR_LOC_CEILING",
    "BUG_SKILL_MD",
    "CASCADE_SOURCES",
    "CONCLUDE_SKILL_MD",
    "DEFAULT_BUG_REPO",
    "INIT_SKILL_MD",
    "MODEL_TIERS",
    "MODEL_TIER_KEY",
    "PLAN_STATUSES",
    "SPECIALIST_TIER_DEFAULTS",
    "UPDATE_SKILL_MD",
    "AuthStatus",
    "BriefingContext",
    "BugReport",
    "CICheck",
    "CascadeChoice",
    "CascadeSource",
    "ContextConfig",
    "ContextThresholds",
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
    "ModelTier",
    "ModelTierConfig",
    "NightlyHostIntegration",
    "PRCIStatus",
    "ParallelismConfig",
    "PlanRecord",
    "PlanStatus",
    "Proposal",
    "Proposer",
    "ProposerCategory",
    "ReasoningEffort",
    "ResolvedDispatch",
    "Run",
    "SpecialistRole",
    "SubAgentResult",
    "SubprocessRunner",
    "TaskDir",
    "TaskOutcome",
    "TierBinding",
    "TodoFixmeProposer",
    "TypeHoleProposer",
    "VerifyCheck",
    "VerifyReport",
    "__version__",
    "all_roles",
    "auto_pr_rejection_reason",
    "bug_gh_command",
    "build_bug_report",
    "build_context",
    "build_task_prompt",
    "can_auto_pr",
    "cascade_next",
    "conclude_run",
    "count_open_nightly_prs",
    "current_run",
    "current_run_pointer",
    "default_proposers",
    "default_subprocess_runner",
    "detect_checks",
    "diagnose_and_repair",
    "fetch_pr_checks",
    "fetch_via_gh",
    "list_ci_status",
    "list_plans",
    "list_runs",
    "load_context_config",
    "load_model_tier_config",
    "load_parallelism_config",
    "new_run_id",
    "new_task",
    "next_task_index",
    "nightly_dir",
    "pick_accepted_rfc",
    "pick_github_issue",
    "pick_ideated",
    "pick_ideated_fallback",
    "pick_in_flight",
    "pick_unblocked",
    "planning_dir",
    "rank_issues",
    "read_plan",
    "render_briefing",
    "repo_root",
    "resolve_context_thresholds",
    "resolve_model_for_task",
    "run_dir",
    "run_loop",
    "run_one_task",
    "run_proposers",
    "run_subprocess",
    "run_verify",
    "runs_dir",
    "score_issue",
    "slugify",
    "specialist_prompt",
    "start_run",
    "submit_bug_report",
    "tier_for_role",
    "top_auto_pr",
    "update_plan_status",
    "write_briefing",
    "write_bug_report",
    "write_drafts",
]
