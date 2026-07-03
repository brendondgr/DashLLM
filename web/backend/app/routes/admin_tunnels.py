"""Control plane: SSH tunnel CRUD, lifecycle, tests, command display."""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas import TunnelCreate, TunnelOut, TunnelPatch, TunnelTestResult
from app.security import admin_guard
from app.services.tunnels import command_string

router = APIRouter(
    prefix="/admin/tunnels", tags=["tunnels"],
    dependencies=[Depends(admin_guard)]
)


@router.get("", response_model=list[TunnelOut])
async def list_tunnels(request: Request):
    return request.app.state.tunnels.list_out()


@router.post("", response_model=TunnelOut, status_code=201)
async def create_tunnel(request: Request, spec: TunnelCreate):
    try:
        row = request.app.state.tunnels.create(spec)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return request.app.state.tunnels.out(row["id"])


@router.patch("/{tid}", response_model=TunnelOut)
async def patch_tunnel(request: Request, tid: str, patch: TunnelPatch):
    try:
        row = await request.app.state.tunnels.patch(tid, patch)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if row is None:
        raise HTTPException(404, "tunnel not found")
    return request.app.state.tunnels.out(tid)


@router.delete("/{tid}", status_code=204)
async def delete_tunnel(request: Request, tid: str):
    if not await request.app.state.tunnels.delete(tid):
        raise HTTPException(404, "tunnel not found")


@router.post("/{tid}/start", response_model=TunnelOut)
async def start_tunnel(request: Request, tid: str):
    out = await request.app.state.tunnels.start(tid)
    if out is None:
        raise HTTPException(404, "tunnel not found")
    return out


@router.post("/{tid}/stop", response_model=TunnelOut)
async def stop_tunnel(request: Request, tid: str):
    out = await request.app.state.tunnels.stop(tid)
    if out is None:
        raise HTTPException(404, "tunnel not found")
    return out


@router.post("/{tid}/test", response_model=TunnelTestResult)
async def test_tunnel(request: Request, tid: str):
    result = await request.app.state.tunnels.test(tid)
    if result is None:
        raise HTTPException(404, "tunnel not found")
    return result


@router.get("/{tid}/command")
async def tunnel_command(request: Request, tid: str):
    row = request.app.state.tunnels.row(tid)
    if row is None:
        raise HTTPException(404, "tunnel not found")
    try:
        return {"command": command_string(row)}
    except ValueError as e:
        raise HTTPException(422, str(e))
