from __future__ import annotations

import typer
from pathlib import Path
from src.config import settings
from src.utils.logging import configure_logging, get_logger

app = typer.Typer(help="LLM-driven autonomous parameter tuning system")
logger = get_logger(__name__)


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    if verbose:
        settings.log_level = "DEBUG"
    configure_logging()


@app.command()
def init_db() -> None:
    """Create database tables and initialize knowledge base."""
    import asyncio
    from src.db.session import create_db_and_tables

    async def _init() -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await create_db_and_tables()
        logger.info("database tables created")

        # Seed knowledge base
        from src.knowledge.retriever import knowledge_base
        from src.knowledge.seed import ALL_SEED_KNOWLEDGE

        await knowledge_base.initialize()
        await knowledge_base.seed(ALL_SEED_KNOWLEDGE)
        logger.info("knowledge base seeded")

    asyncio.run(_init())


@app.command()
def run(
    experiment_config: Path = typer.Argument(
        ..., help="Path to experiment YAML config file", exists=True
    ),
    max_trials: int = typer.Option(0, "--max-trials", "-n", help="Override max trials"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse config and print plan without executing"),
    resume: bool = typer.Option(False, "--resume", "-r", help="Resume from last interrupted experiment"),
    host: str = typer.Option("", "--host", "-h", help="Redis host (skip Docker provisioning)"),
    port: str = typer.Option("6379", "--port", "-p", help="Redis port"),
    password: str = typer.Option("", "--password", "-a", help="Redis password"),
    config_file: str = typer.Option("", "--config-file", help="Path to local redis.conf"),
    bo_trials: int = typer.Option(0, "--bo-trials", help="Bayesian Optimization trials after LLM phase (0=skip)"),
    stable: bool = typer.Option(False, "--stable", help="Enable warmup + multi-iteration median benchmark"),
    stable_iterations: int = typer.Option(3, "--stable-iterations", help="Benchmark iterations for stable mode"),
    benchmark_cmd: str = typer.Option(
        "", "--benchmark-cmd",
        help="Benchmark shell command. Placeholders: {host}, {port}, {config_path}",
    ),
) -> None:
    """Run an optimization experiment.

    To tune an already-running Redis without Docker:

        python -m src.main run config.yaml \\
            --host 127.0.0.1 --port 6379 \\
            --config-file /path/to/redis.conf \\
            --benchmark-cmd "redis-benchmark -h {host} -p {port} -c 50 -n 100000 --csv"

    Without --host, the project provisions its own Redis container via Docker.
    """
    import asyncio
    import yaml

    async def _run() -> None:
        from src.workflow.state import ExperimentState, GoalSpec, TrialResult
        from src.utils.interrupt import install_handler

        # ── Resume path ──────────────────────────────────────────────────
        if resume:
            from src.utils.interrupt import load_snapshot, has_snapshot

            if not has_snapshot():
                logger.error("no interrupt snapshot found — cannot resume")
                raise typer.Exit(code=1)

            snap = load_snapshot()
            if snap is None:
                logger.error("failed to load snapshot")
                raise typer.Exit(code=1)

            logger.info(
                "resuming from snapshot",
                experiment=snap.get("experiment_name"),
                trial=snap.get("trial_number"),
            )

            state = ExperimentState(
                experiment_name=snap.get("experiment_name", "unnamed"),
                target_system=snap.get("target_system", "redis"),
                target_version=snap.get("target_version", ""),
                goals=[
                    GoalSpec(**g) for g in snap.get("goals", [])
                ],
                current_config=snap.get("current_config", {}),
                baseline_config=snap.get("baseline_config", {}),
                best_config=snap.get("best_config", {}),
                best_metrics=snap.get("best_metrics", {}),
                best_trial_number=snap.get("best_trial_number", 0),
                container_id=snap.get("container_id", ""),
                trial_number=snap.get("trial_number", 0),
                improvement_history=snap.get("improvement_history", []),
                rollback_history=snap.get("rollback_history", []),
                consecutive_rollbacks=snap.get("consecutive_rollbacks", 0),
                hardware_spec=snap.get("hardware_spec", {}),
                max_trials=max_trials or snap.get("max_trials", 30),
            )

            # Restore trial_history as TrialResult objects
            state.trial_history = []
            for td in snap.get("trial_history", []):
                tr = TrialResult(
                    trial_number=td["trial_number"],
                    config=td.get("config", {}),
                    metrics=td.get("metrics", {}),
                    benchmark_results=td.get("benchmark_results", []),
                    parameter_changes=td.get("parameter_changes", []),
                    improvement_pct=td.get("improvement_pct", 0.0),
                    status=td.get("status", "unknown"),
                )
                state.trial_history.append(tr)

            # Delete snapshot so we don't accidentally resume again
            from src.utils.interrupt import SNAPSHOT_PATH
            SNAPSHOT_PATH.unlink(missing_ok=True)

        # ── Fresh start ──────────────────────────────────────────────────
        else:
            with open(experiment_config) as f:
                cfg = yaml.safe_load(f)

            goals = [
                GoalSpec(
                    metric=g["metric"],
                    operator=g.get("operator", ">="),
                    value=float(g["value"]),
                    weight=float(g.get("weight", 1.0)),
                )
                for g in cfg.get("goals", [])
            ]

            opt = cfg.get("optimization", {})
            safety = cfg.get("safety", {})

            state = ExperimentState(
                experiment_name=cfg.get("name", "unnamed"),
                target_system=cfg.get("target_system", "redis"),
                target_version=cfg.get("target_version", ""),
                goals=goals,
                max_trials=max_trials or opt.get("max_trials", 30),
                max_duration_hours=opt.get("max_duration_hours", 8.0),
                max_changes_per_trial=opt.get("max_changes_per_trial", 4),
                convergence_window=opt.get("convergence_window", 5),
                improvement_threshold_pct=opt.get("improvement_threshold_pct", 2.0),
                max_restart_changes=safety.get("max_restart_requiring_changes", 2),
                max_consecutive_rollbacks=safety.get("max_consecutive_rollbacks", 3),
                memory_headroom_pct=safety.get("memory_headroom_pct", 20),
                blocklist=opt.get("parameter_focus", {}).get("blocklist", []),
                stable_mode=stable,
                stable_iterations=stable_iterations,
            )

            logger.info("experiment config loaded", name=state.experiment_name, goals=len(goals))

            if dry_run:
                _print_dry_run(state)
                return

            # ── Direct-connect mode (--host provided) ────────────────────
            if host:
                await _setup_direct_mode(state, host, port, password,
                                         config_file, benchmark_cmd)

            # ── Docker mode (default) ────────────────────────────────────
            else:
                from src.environment.manager import TargetEnvironmentManager, EnvironmentConfig

                env_config = EnvironmentConfig(
                    template=cfg.get("environment", {}).get("template", "redis-standalone"),
                    cpu_limit=cfg.get("environment", {}).get("cpu_limit", "4"),
                    memory_limit=cfg.get("environment", {}).get("memory_limit", "8g"),
                    docker_image=cfg.get("environment", {}).get("docker_image", ""),
                )

                env_mgr = TargetEnvironmentManager()
                container = await env_mgr.provision(env_config, state.experiment_id)
                state.container_id = container.container_id

                config_path_map = {
                    "redis": "/usr/local/etc/redis/redis.conf",
                    "mysql": "/etc/mysql/my.cnf",
                }
                config_path = config_path_map.get(state.target_system, "")
                if config_path and container.container_id:
                    raw_config = await env_mgr.get_config(config_path)
                    if raw_config:
                        from src.parameters.manager import ParameterManager
                        pm = ParameterManager(state.target_system)
                        state.current_config = pm.parse_and_validate(raw_config)
                        state.baseline_config = dict(state.current_config)

        # ── Run workflow (shared by all paths) ─────────────────────────
        install_handler()
        state = await _run_workflow(state)

        # ── Bayesian Optimization continuation ──────────────────────────
        if bo_trials > 0 and state.trial_history:
            await _run_bayesian_phase(state, bo_trials)

    asyncio.run(_run())


async def _setup_direct_mode(
    state, host: str, port: str, password: str,
    config_file: str, benchmark_cmd: str,
) -> None:
    """Configure state for direct-connect mode (no Docker)."""
    from src.benchmark.direct_runner import DirectRedisRunner

    if not config_file:
        typer.echo("Error: --config-file is required when using --host", err=True)
        raise typer.Exit(code=1)

    state.direct_mode = True
    state.redis_host = host
    state.redis_port = port
    state.redis_password = password
    state.direct_config_path = config_file
    state.direct_benchmark_cmd = benchmark_cmd

    runner = DirectRedisRunner(
        config_path=config_file,
        host=host,
        port=port,
        password=password,
        benchmark_cmd=benchmark_cmd,
    )
    state.container_id = f"direct-{host}:{port}"

    # Read baseline config from local file
    raw_config = await runner.read_config()
    if raw_config:
        from src.parameters.manager import ParameterManager
        pm = ParameterManager(state.target_system)
        state.current_config = pm.parse_and_validate(raw_config)
        state.baseline_config = dict(state.current_config)
        logger.info("baseline config loaded from", path=config_file, params=len(state.current_config))
    else:
        logger.error("could not read config from", path=config_file)
        raise typer.Exit(code=1)

    # Quick health check
    if not await runner.health_check():
        logger.warning("Redis health check failed — continuing anyway")


async def _run_workflow(state) -> "ExperimentState":
    """Run the workflow loop with graceful interrupt handling.

    Returns the final ExperimentState reconstructed from the last event
    (since LangGraph astream yields dict copies, not the original object).
    """
    import asyncio
    from datetime import datetime
    from src.workflow.graph import create_workflow
    from src.workflow.state import ExperimentState, TrialResult, GoalSpec
    from src.utils.interrupt import (
        is_interrupted,
        is_force_exit,
        save_snapshot,
        generate_llm_summary,
        write_minimal_summary,
        SNAPSHOT_PATH,
    )

    workflow = create_workflow()
    logger.info("starting optimization loop", trials=state.max_trials)

    final_state = None
    try:
        async for event in workflow.astream(state, stream_mode="values"):
            node_name = event.get("phase", "unknown")
            logger.info("workflow step", phase=node_name)
            final_state = event

            if is_interrupted():
                logger.info("interrupt flag detected — exiting loop gracefully")
                break

    except asyncio.CancelledError:
        logger.info("workflow cancelled — saving progress")

    except Exception:
        logger.exception("workflow error — saving progress before exit")

    # Always save best-effort state
    current = final_state if final_state else state
    await _handle_interrupt(current)

    # Reconstruct ExperimentState from final event dict
    if final_state and isinstance(final_state, dict):
        return _reconstruct_state(state, final_state)
    return state


async def _handle_interrupt(state) -> None:
    """Save snapshot and optionally generate LLM summary on interrupt."""
    from datetime import datetime
    from src.utils.interrupt import (
        is_interrupted,
        is_force_exit,
        save_snapshot,
        generate_llm_summary,
        write_minimal_summary,
        SNAPSHOT_PATH,
    )

    if not is_interrupted():
        return

    interrupted_at = datetime.utcnow()

    # Step 1: Always save the data snapshot (fast, synchronous in spirit)
    logger.info("saving experiment snapshot...")
    try:
        save_snapshot(state, interrupted_at=interrupted_at)
    except Exception as exc:
        logger.error("failed to save snapshot", error=str(exc))
        return

    # Step 2: Try LLM summary, fall back to minimal summary
    snap = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else None

    if snap and not is_force_exit():
        typer.echo("\nGenerating LLM progress summary (Ctrl+C to skip)...")
        try:
            llm_text = await generate_llm_summary(snap)
            if llm_text and not is_force_exit():
                summary_path = SNAPSHOT_PATH.with_name("interrupt_summary_llm.md")
                summary_path.write_text(llm_text)
                typer.echo(f"\nLLM summary saved to {summary_path}")
                typer.echo("\n" + llm_text[:500] + ("..." if len(llm_text) > 500 else ""))
                return
        except Exception as exc:
            logger.warning("LLM summary failed, using minimal summary", error=str(exc)[:100])

    # Fallback: minimal summary from just the snapshot data
    if snap:
        sp = write_minimal_summary(snap)
        typer.echo(f"\nMinimal summary saved to {sp}")
    else:
        typer.echo(f"\nSnapshot saved to {SNAPSHOT_PATH}")

    typer.echo(f"\nResume with: python -m src.main run <config.yaml> --resume")


async def _run_bayesian_phase(state, bo_trials: int) -> None:
    """Run Bayesian Optimization continuation after the LLM phase finishes.

    Seeds the optimizer with all LLM trial history, then runs ``bo_trials``
    additional trials using the selected backend (GP+EI or TPE).
    """
    import json
    from src.optimization.selector import select_backend
    from src.parameters.manager import ParameterManager
    from src.parameters.schema import ParameterRisk

    pm = ParameterManager(state.target_system)
    bo_params = [
        p for p in pm.get_tunable_parameters(max_risk=ParameterRisk.MEDIUM)
        if p.name not in getattr(state, "blocklist", [])
        and p.type in ("integer", "float", "enum", "boolean")
    ]
    if not bo_params:
        logger.warning("no parameters available for Bayesian optimization")
        return

    # Build history dicts for seeding
    history_dicts = [
        {"config": t.config, "metrics": t.metrics}
        for t in state.trial_history if t.metrics
    ]
    if len(history_dicts) < 2:
        logger.warning("insufficient trial history for Bayesian seeding")
        return

    # Build objective: write config → run benchmark → return score
    if state.direct_mode:
        from src.benchmark.direct_runner import DirectRedisRunner
        runner = DirectRedisRunner(
            config_path=state.direct_config_path,
            host=state.redis_host, port=state.redis_port,
            password=state.redis_password,
            benchmark_cmd=state.direct_benchmark_cmd,
        )
    else:
        from src.benchmark.runner import BenchmarkRunner
        runner = BenchmarkRunner.for_system(state.target_system, state.container_id)

    from src.benchmark.runner import BenchmarkProfile

    profile = BenchmarkProfile.from_dict({
        "name": "bo-phase",
        "runner_type": "redis_benchmark" if state.target_system == "redis" else "sysbench",
        "tests": ["set", "get"] if state.target_system == "redis" else ["oltp_read_write"],
        "clients": 50, "requests": 100000, "duration_sec": 30,
    })

    async def bo_objective(config: dict[str, str]) -> float:
        full_config = dict(state.current_config)
        full_config.update(config)
        config_text = pm.serialize_config(full_config)

        if state.direct_mode:
            await runner.write_config(config_text)
        else:
            from src.environment.manager import TargetEnvironmentManager
            env_mgr = TargetEnvironmentManager.for_container(state.container_id)
            config_path_map = {
                "redis": "/usr/local/etc/redis/redis.conf",
                "mysql": "/etc/mysql/my.cnf",
            }
            config_path = config_path_map.get(state.target_system, "/etc/config.conf")
            await env_mgr.apply_config(config_text, config_path, restart=False)

        metrics = await runner.run(profile)
        score = metrics.get_metric("total_rps") or metrics.get_metric("qps") or 0.0
        return float(score)

    # Select backend and run
    optimizer = select_backend(param_defs=bo_params, objective_fn=bo_objective)
    optimizer.seed_from_history(history_dicts, maximize=True)

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  BAYESIAN OPTIMIZATION PHASE")
    typer.echo(f"  {bo_trials} iterations | Backend: {type(optimizer).__name__}")
    typer.echo(f"  Seeded from {len(history_dicts)} LLM trials")
    typer.echo(f"{'=' * 60}\n")

    bo_result = await optimizer.optimize(maximize=True, n_calls=bo_trials, verbose=True)

    # Show combined results
    llm_best = max(
        (t.improvement_pct for t in state.trial_history if t.improvement_pct),
        default=100.0,
    )
    bo_best = bo_result.get("best_score", 0.0)

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  COMBINED RESULTS")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  LLM Trials:     {len(state.trial_history)}")
    typer.echo(f"  BO Trials:      {bo_result.get('n_total', 0)}")
    typer.echo(f"  BO Best Score:  {bo_best:.1f}")
    if bo_result.get("best_config"):
        typer.echo(f"\n  Best Config (LLM + BO):")
        for k, v in list(bo_result["best_config"].items())[:10]:
            typer.echo(f"    {k} = {v}")

    # Record best BO config in state
    if bo_result.get("best_config") and bo_best > 0:
        state.best_config = dict(state.current_config)
        state.best_config.update(bo_result["best_config"])


def _reconstruct_state(original: "ExperimentState", event: dict) -> "ExperimentState":
    """Build an ExperimentState from the original + the last astream event dict."""
    from src.workflow.state import ExperimentState, TrialResult, GoalSpec

    # Copy mutable fields from the event dict back into the original object
    if "trial_number" in event:
        original.trial_number = event["trial_number"]
    if "current_config" in event:
        original.current_config = event["current_config"]
    if "best_config" in event:
        original.best_config = event["best_config"]
    if "best_metrics" in event:
        original.best_metrics = event["best_metrics"]
    if "best_trial_number" in event:
        original.best_trial_number = event["best_trial_number"]
    if "improvement_history" in event:
        original.improvement_history = event["improvement_history"]
    if "consecutive_rollbacks" in event:
        original.consecutive_rollbacks = event["consecutive_rollbacks"]
    if "rollback_history" in event:
        original.rollback_history = event["rollback_history"]
    if "container_id" in event:
        original.container_id = event["container_id"]
    if "elapsed_hours" in event:
        original.elapsed_hours = event["elapsed_hours"]
    if "phase" in event:
        original.phase = event["phase"]
    if "errors" in event:
        original.errors = event["errors"]

    # Reconstruct trial_history from event (handles both dicts and TrialResult objects)
    if "trial_history" in event and event["trial_history"]:
        original.trial_history = []
        for td in event["trial_history"]:
            if isinstance(td, dict):
                tr = TrialResult(
                    trial_number=td.get("trial_number", 0),
                    config=td.get("config", {}),
                    metrics=td.get("metrics", {}),
                    benchmark_results=td.get("benchmark_results", []),
                    parameter_changes=td.get("parameter_changes", []),
                    improvement_pct=td.get("improvement_pct", 0.0),
                    status=td.get("status", "unknown"),
                )
                original.trial_history.append(tr)
            elif hasattr(td, "trial_number"):
                # Already a TrialResult or compatible object
                original.trial_history.append(td)

    if "current_trial" in event and event["current_trial"]:
        ct = event["current_trial"]
        if isinstance(ct, dict):
            original.current_trial = TrialResult(
                trial_number=ct.get("trial_number", 0),
                config=ct.get("config", {}),
                metrics=ct.get("metrics", {}),
                benchmark_results=ct.get("benchmark_results", []),
                parameter_changes=ct.get("parameter_changes", []),
                improvement_pct=ct.get("improvement_pct", 0.0),
                status=ct.get("status", "unknown"),
            )
        elif hasattr(ct, "trial_number"):
            original.current_trial = ct

    return original


def _print_dry_run(state: ExperimentState) -> None:
    """Print experiment plan without executing."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"[bold]Experiment: {state.experiment_name}[/bold]")
    console.print(f"Target: {state.target_system} {state.target_version}")
    console.print(f"Max Trials: {state.max_trials} | Max Duration: {state.max_duration_hours}h")

    table = Table(title="Goals")
    table.add_column("Metric", style="cyan")
    table.add_column("Target", style="green")
    table.add_column("Weight", style="yellow")
    for g in state.goals:
        table.add_row(g.metric, f"{g.operator} {g.value}", str(g.weight))
    console.print(table)

    console.print(f"\nConvergence Window: {state.convergence_window} trials")
    console.print(f"Improvement Threshold: {state.improvement_threshold_pct}%")
    console.print(f"Max Changes/Trial: {state.max_changes_per_trial}")
    console.print("[yellow]Dry run — no changes will be made.[/yellow]")


@app.command()
def list_systems() -> None:
    """List supported target systems."""
    from src.parameters.schema import SUPPORTED_SYSTEMS
    for system in SUPPORTED_SYSTEMS:
        typer.echo(f"  - {system}")


@app.command()
def show_params(
    target_system: str = typer.Argument(..., help="Target system (redis, mysql)"),
) -> None:
    """Show known parameters for a target system."""
    import json

    schema_path = Path(__file__).parent / "parameters" / "schemas" / f"{target_system}.json"
    if not schema_path.exists():
        available = [p.stem for p in (Path(__file__).parent / "parameters" / "schemas").glob("*.json")]
        typer.echo(f"No schema found for '{target_system}'. Available: {available}", err=True)
        raise typer.Exit(code=1)

    data = json.loads(schema_path.read_text())
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


@app.command()
def seed_kb() -> None:
    """Seed the knowledge base with initial data."""
    import asyncio
    from src.knowledge.retriever import knowledge_base
    from src.knowledge.seed import ALL_SEED_KNOWLEDGE

    async def _seed() -> None:
        await knowledge_base.initialize()
        await knowledge_base.seed(ALL_SEED_KNOWLEDGE)
        logger.info("knowledge base seeded", entries=len(ALL_SEED_KNOWLEDGE))

    asyncio.run(_seed())


@app.command()
def query_kb(
    query: str = typer.Argument(..., help="Search query"),
    system: str = typer.Option("", "--system", "-s", help="Filter by system (redis, mysql)"),
    top_n: int = typer.Option(5, "--top", "-n", help="Number of results"),
) -> None:
    """Query the knowledge base."""
    import asyncio
    from src.knowledge.retriever import knowledge_base

    async def _query() -> None:
        await knowledge_base.initialize()
        results = await knowledge_base.query(query, system=system or None, n_results=top_n)
        for i, entry in enumerate(results, 1):
            typer.echo(f"\n--- Result {i} ---")
            typer.echo(f"Parameter: {entry.parameter_name}")
            typer.echo(f"Category: {entry.category}")
            typer.echo(f"Title: {entry.title}")
            typer.echo(f"Content: {entry.content[:200]}...")

    asyncio.run(_query())


@app.command("history")
def list_history(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of experiments to show"),
) -> None:
    """List recent experiments from the database."""
    import asyncio
    from src.tracking.experiment import ExperimentTracker

    async def _list() -> None:
        experiments = await ExperimentTracker.list_experiments(limit=limit)
        if not experiments:
            typer.echo("No experiments found. Run an experiment first.")
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="Experiment History")
        table.add_column("ID", style="dim")
        table.add_column("Name", style="cyan")
        table.add_column("Target", style="green")
        table.add_column("Trials")
        table.add_column("Status")
        table.add_column("Started")

        for exp in experiments:
            table.add_row(
                exp.id[:8] if exp.id else "?",
                exp.name or "?",
                f"{exp.target_system or '?'} {exp.target_version or ''}",
                str(exp.current_trial or 0),
                exp.status or "?",
                exp.created_at.strftime("%Y-%m-%d %H:%M") if exp.created_at else "?",
            )

        console.print(table)
        typer.echo(f"\nView details: python -m src.main show <experiment_id>")

    asyncio.run(_list())


@app.command("show")
def show_experiment(
    experiment_id: str = typer.Argument(..., help="Experiment ID (first 8 chars suffice)"),
) -> None:
    """Show full details of an experiment including all trials."""
    import asyncio
    from src.tracking.experiment import ExperimentTracker

    async def _show() -> None:
        from rich.console import Console
        from rich.table import Table
        import json

        console = Console()

        # Find full experiment by prefix ID
        experiments = await ExperimentTracker.list_experiments(limit=100)
        match = None
        for exp in experiments:
            if exp.id and exp.id.startswith(experiment_id):
                match = exp
                break

        if match is None:
            typer.echo(f"No experiment found with ID starting with '{experiment_id}'", err=True)
            raise typer.Exit(code=1)

        exp = match

        # Header
        console.print(f"\n[bold cyan]Experiment: {exp.name}[/bold cyan]")
        console.print(f"  ID: {exp.id}")
        console.print(f"  Target: {exp.target_system} {exp.target_version or ''}")
        console.print(f"  Status: {exp.status}")
        console.print(f"  Created: {exp.created_at.strftime('%Y-%m-%d %H:%M:%S') if exp.created_at else '?'}")
        if exp.finished_at:
            console.print(f"  Finished: {exp.finished_at.strftime('%Y-%m-%d %H:%M:%S')}")

        # Goals
        if exp.goals_json:
            goals = json.loads(exp.goals_json)
            console.print("\n[bold]Goals:[/bold]")
            for g in goals:
                console.print(f"  - {g['metric']} {g['operator']} {g['value']} (weight: {g.get('weight', 1.0)})")

        # Best metrics
        if exp.best_metrics_json:
            best = json.loads(exp.best_metrics_json)
            console.print("\n[bold]Best Results:[/bold]")
            for k, v in best.items():
                console.print(f"  {k}: {v}")

        # Trials
        from src.db.session import async_session as _async_session
        async with _async_session() as session:
            tracker = ExperimentTracker(session)
            trials = await tracker.get_trials(exp.id)

            if not trials:
                console.print("\n[yellow]No trials recorded[/yellow]")
            else:
                console.print(f"\n[bold]Trials ({len(trials)}):[/bold]")
                table = Table(title="")
                table.add_column("#", style="dim")
                table.add_column("Status")
                table.add_column("Improvement")
                table.add_column("Changes")
                table.add_column("Key Metrics")

                for t in trials:
                    metrics = json.loads(t.metrics_json or "{}")
                    changes = json.loads(t.config_snapshot_json or "{}")

                    # Pick top 2 metrics to show
                    metric_parts = []
                    for k, v in list(metrics.items())[:2]:
                        if isinstance(v, float):
                            metric_parts.append(f"{k}: {v:.1f}")
                        else:
                            metric_parts.append(f"{k}: {v}")

                    # Pick changed params
                    param_parts = []
                    for c in (t.parameter_changes or [])[:2]:
                        param_parts.append(f"{c.parameter_name}: {c.old_value}→{c.new_value}")

                    table.add_row(
                        str(t.trial_number),
                        t.status or "?",
                        f"{t.improvement_pct:+.1f}%" if t.improvement_pct else "?",
                        ", ".join(param_parts) or "-",
                        ", ".join(metric_parts) or "-",
                    )

                console.print(table)

        # Baseline config
        if exp.baseline_config_json:
            console.print("\n[bold]Baseline Config:[/bold]")
            baseline = json.loads(exp.baseline_config_json)
            for k, v in list(baseline.items())[:10]:
                console.print(f"  {k} = {v}")
            if len(baseline) > 10:
                console.print(f"  ... and {len(baseline) - 10} more")

    asyncio.run(_show())


# ── Skill management ──────────────────────────────────────────────────────


@app.command("skill")
def skill_list(
    list_all: bool = typer.Option(False, "--list", "-l", help="List all loaded skills"),
) -> None:
    """List user-defined skills."""
    from pathlib import Path
    from src.workflow.skill import SkillLoader, SKILLS_DIR

    if not SKILLS_DIR.exists():
        typer.echo("No skills/ directory found.")
        typer.echo("Create one with: mkdir skills/")
        return

    loader = SkillLoader()
    skills = loader.load_all(include_disabled=True)
    entries = list(SKILLS_DIR.iterdir())

    if not entries:
        typer.echo(f"No files in {SKILLS_DIR}/")
        typer.echo("Add a .py or .yaml skill file, then re-run.")
        return

    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title=f"Skills ({SKILLS_DIR}/)")

    table.add_column("File", style="dim")
    table.add_column("Name")
    table.add_column("Node")
    table.add_column("Phase")
    table.add_column("Type")
    table.add_column("Status")

    for entry in sorted(entries):
        if entry.name.startswith("__"):
            continue
        matched = [s for s in skills if s.name.replace("-", "_") in entry.stem.replace("-", "_") or entry.stem.replace("-", "_") in s.name.replace("-", "_")]
        if matched:
            s = matched[0]
            table.add_row(
                entry.name, s.name, s.node, s.phase,
                "python" if entry.suffix == ".py" else "yaml",
                "[green]active" if s.enabled else "[dim]disabled",
            )
        else:
            table.add_row(
                entry.name, entry.stem, "?", "?",
                "python" if entry.suffix == ".py" else "yaml",
                "[red]error",
            )

    console.print(table)

    if not any(not entry.name.startswith("__") for entry in entries):
        typer.echo("\nNo skill files found. Add a .py or .yaml file to skills/")

    typer.echo(f"\nSkill files: {SKILLS_DIR.resolve()}")
    typer.echo("Edit .py skills and set enabled=True/False to toggle.")
    typer.echo("YAML skills: change 'enabled: false' to 'enabled: true'.")


if __name__ == "__main__":
    app()
