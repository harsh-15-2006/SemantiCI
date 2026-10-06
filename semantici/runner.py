"""Executes business scenarios (workflows) against the running application."""
import re

import httpx

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _subst(value, ctx):
    if isinstance(value, str):
        whole = _VAR.fullmatch(value)
        if whole:
            return ctx.get(whole.group(1))
        return _VAR.sub(lambda m: str(ctx.get(m.group(1), "")), value)
    if isinstance(value, dict):
        return {k: _subst(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, ctx) for v in value]
    return value


def run_workflow(base_url: str, workflow: dict) -> dict:
    """Runs every step in order and stops at the first step that fails."""
    ctx, steps = {}, []
    with httpx.Client(base_url=base_url, timeout=20) as client:
        for step in workflow.get("steps") or []:
            method = str(step.get("method", "GET")).upper()
            path = _subst(step.get("path", "/"), ctx)
            body = _subst(step.get("json"), ctx)
            result = {"method": method, "path": path, "request": body}
            try:
                r = client.request(method, path, json=body)
            except httpx.HTTPError as e:
                result.update(status=None, ok=False, response=f"request failed: {e}")
                steps.append(result)
                break
            expect = step.get("expect_status")
            if isinstance(expect, list):
                ok = r.status_code in expect
            else:
                ok = r.status_code == expect if expect else r.status_code < 400
            try:
                data = r.json()
            except ValueError:
                data = r.text[:500]
            for var, field in (step.get("save") or {}).items():
                if isinstance(data, dict):
                    ctx[var] = data.get(field)
            result.update(status=r.status_code, ok=ok, response=data)
            steps.append(result)
            if not ok:
                break
    return {"name": workflow.get("name", "workflow"), "ok": bool(steps) and all(s["ok"] for s in steps), "steps": steps}
