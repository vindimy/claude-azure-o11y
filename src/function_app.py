"""Azure Functions v2 entry point: one timer per run mode, schedules from app settings."""

from __future__ import annotations

import azure.functions as func

from bootstrap import main

app = func.FunctionApp()


@app.timer_trigger(
    schedule="%OPS_SCHEDULE_CRON%", arg_name="timer", run_on_startup=False, use_monitor=True
)
async def o11y_ops(timer: func.TimerRequest) -> None:
    await main("ops")


@app.timer_trigger(
    schedule="%FINOPS_SCHEDULE_CRON%", arg_name="timer", run_on_startup=False, use_monitor=True
)
async def o11y_finops(timer: func.TimerRequest) -> None:
    await main("finops")
