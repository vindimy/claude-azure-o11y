"""Azure Functions v2 entry point. Schedule comes from the SCHEDULE_CRON app setting."""

from __future__ import annotations

import azure.functions as func

from bootstrap import main

app = func.FunctionApp()


@app.timer_trigger(
    schedule="%SCHEDULE_CRON%", arg_name="timer", run_on_startup=False, use_monitor=True
)
async def o11y_evaluate(timer: func.TimerRequest) -> None:
    await main()
