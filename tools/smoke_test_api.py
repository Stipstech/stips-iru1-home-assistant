"""One-off smoke test for STIPS API (run from repo root: python -m ...)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import aiohttp

# scripts -> stips_iru1 -> custom_components -> homeassistant -> repo_root
_REPO = Path(__file__).resolve().parents[4]
_PKG_DIR = _REPO / "homeassistant" / "custom_components" / "stips_iru1"
_pkg = types.ModuleType("stips_iru1")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["stips_iru1"] = _pkg

def _load(name: str, fname: str):
    path = _PKG_DIR / fname
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_load("stips_iru1.const", "const.py")
_api_mod = _load("stips_iru1.api", "api.py")
StipsApiClient = _api_mod.StipsApiClient

HOST = "stips.api.stagging.visionalization.net"
USER = "st"
PWD = "111111"


def _area_id(area: dict) -> int | None:
    v = area.get("id") if "id" in area else area.get("areaId")
    return int(v) if v is not None else None


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        c = StipsApiClient(host=HOST, session=session)
        print("1) Login...")
        await c.login(USER, PWD)
        print("   OK")
        print("2) areas/Browse...")
        areas = await c.get_areas()
        print(f"   areas count: {len(areas)}")
        if not areas:
            print("   FAIL: no areas")
            return
        a0 = areas[0]
        print(f"   first area keys sample: {list(a0.keys())[:12]}")
        aid = _area_id(a0)
        if aid is None:
            print("   FAIL: no id or areaId")
            return
        print(f"   first area_id={aid}")
        print("3) IrRemote/Devices/Browse...")
        devices = await c.get_devices(aid)
        print(f"   devices in first area: {len(devices)}")
        if not devices:
            for ar in areas:
                ai = _area_id(ar)
                if ai is None:
                    continue
                devs = await c.get_devices(ai)
                if devs:
                    aid = ai
                    devices = devs
                    print(f"   found {len(devs)} devices in area {aid}")
                    break
        if not devices:
            print("   SKIP: no IR devices in any area scanned")
            return
        un = devices[0].get("uniqueName")
        print(f"4) IrRemote/Devices/read uniqueName={un!r}...")
        full = await c.read_device(aid, un)
        if not full:
            print("   FAIL read_device")
            return
        remotes = full.get("remotes") or []
        print(f"   remotes: {len(remotes)}")
        for r in remotes[:5]:
            t = r.get("type")
            mid = r.get("modelId")
            has_model = r.get("model") is not None
            print(f"     - type={t!r} modelId={mid!r} has_model={has_model}")
        for r in remotes:
            if r.get("model") is None and r.get("modelId"):
                rt = str(r.get("type") or "")
                nt = rt.lower().replace("learned", "")
                if nt:
                    print(f"5) irRemote/Models/read type={nt!r}...")
                    try:
                        m = await c.read_model(str(r["modelId"]), nt)
                        print(f"   model present: {bool(m)}, keys: {list(m.keys())[:10] if m else []}")
                    except Exception as exc:
                        print(f"   Models/read skipped/failed (expected for some TV ids): {exc}")
                break
        print("DONE: smoke OK")


if __name__ == "__main__":
    asyncio.run(main())
