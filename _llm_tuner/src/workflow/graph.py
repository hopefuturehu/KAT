"""LangGraph state machine for the optimization loop.

User-defined skills (from ``skills/``) are loaded automatically and
wrapped around each workflow node as pre/post hooks.
"""

from typing import Literal
from langgraph.graph import StateGraph, END
from src.workflow.state import ExperimentState, ExperimentPhase
from src.workflow.nodes.initialize import initialize_experiment
from src.workflow.nodes.plan import plan_changes
from src.workflow.nodes.safety_check import safety_gate
from src.workflow.nodes.apply_config import apply_configuration
from src.workflow.nodes.run_benchmark import execute_benchmark
from src.workflow.nodes.analyze import analyze_results
from src.workflow.nodes.decide import make_decision
from src.workflow.nodes.rollback import rollback_config
from src.workflow.nodes.finalize import finalize_experiment
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ── Node registry (name → async callable) ───────────────────────────────────

_NODE_REGISTRY: dict[str, object] = {
    "initialize": initialize_experiment,
    "plan": plan_changes,
    "safety_check": safety_gate,
    "apply_config": apply_configuration,
    "run_benchmark": execute_benchmark,
    "analyze": analyze_results,
    "decide": make_decision,
    "rollback": rollback_config,
    "finalize": finalize_experiment,
}


def _wrap_with_skills(node_fn, node_name: str, skills: list) -> object:
    """Wrap *node_fn* so that matching pre/post skills run around it."""
    pre = [s for s in skills if s.node == node_name and s.phase == "pre"]
    post = [s for s in skills if s.node == node_name and s.phase == "post"]

    if not pre and not post:
        return node_fn

    async def _wrapped(state: ExperimentState) -> ExperimentState:
        for skill in pre:
            try:
                state = await skill.run(state)
            except Exception as exc:
                logger.error("skill pre-hook failed", skill=skill.name, error=str(exc))
        state = await node_fn(state)
        for skill in post:
            try:
                state = await skill.run(state)
            except Exception as exc:
                logger.error("skill post-hook failed", skill=skill.name, error=str(exc))
        return state

    _wrapped.__name__ = node_fn.__name__  # preserve name for LangGraph
    logger.info("node wrapped with skills", node=node_name, pre=len(pre), post=len(post))
    return _wrapped


def build_optimization_graph() -> StateGraph:
    from src.workflow.skill import SkillLoader

    skills = SkillLoader().load_all()

    workflow = StateGraph(ExperimentState)

    # Wrap each node with user skills, then add to graph
    for name, fn in _NODE_REGISTRY.items():
        wrapped = _wrap_with_skills(fn, name, skills)
        workflow.add_node(name, wrapped)

    # Entry point
    workflow.set_entry_point("initialize")

    # Edges
    workflow.add_edge("initialize", "plan")

    # Plan -> Safety Check (if changes proposed) or Decide (if no changes)
    workflow.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "safety_check": "safety_check",
            "decide": "decide",
        },
    )

    # Safety Check -> Apply Config (if approved) or Plan (if rejected)
    workflow.add_conditional_edges(
        "safety_check",
        route_after_safety,
        {
            "apply_config": "apply_config",
            "plan": "plan",
        },
    )

    workflow.add_edge("apply_config", "run_benchmark")
    workflow.add_edge("run_benchmark", "analyze")
    workflow.add_edge("analyze", "decide")

    # Decide -> Plan (continue), Finalize (done), Rollback (revert)
    workflow.add_conditional_edges(
        "decide",
        route_after_decide,
        {
            "plan": "plan",
            "finalize": "finalize",
            "rollback": "rollback",
        },
    )

    workflow.add_edge("rollback", "plan")
    workflow.add_edge("finalize", END)

    return workflow


def route_after_plan(state: ExperimentState) -> Literal["safety_check", "decide"]:
    proposal = state.tuning_proposal
    if proposal and proposal.get("changes"):
        return "safety_check"
    return "decide"


def route_after_safety(state: ExperimentState) -> Literal["apply_config", "plan"]:
    verdict = state.safety_verdict
    if verdict.get("verdict") in ("APPROVE", "APPROVE_WITH_MODIFICATIONS"):
        return "apply_config"
    return "plan"


def route_after_decide(state: ExperimentState) -> Literal["plan", "finalize", "rollback"]:
    decision = state.orchestrator_decision
    action = decision.get("action", "CONTINUE_TUNING")

    if action == "ROLLBACK":
        return "rollback"

    if action in ("CONVERGED", "MAX_TRIALS_REACHED", "MAX_DURATION_REACHED", "GOALS_MET"):
        return "finalize"

    return "plan"


def create_workflow():
    """Create a compiled workflow with SQLite checkpointing."""
    graph = build_optimization_graph()
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        import sqlite3
        conn = sqlite3.connect("data/workflow_checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        return graph.compile(checkpointer=checkpointer)
    except ImportError:
        logger.warning("SqliteSaver not available — running without checkpointing")
        return graph.compile()
