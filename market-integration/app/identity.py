"""
Who is making a request, for per-user data such as watchlists (#26).

Until Symphony authentication lands, users are anonymous: the
X-User-Id header if a client sends one (scripts, tests, a proxy that
already knows the user), otherwise an ID kept in the USER_COOKIE cookie,
issued on the first request that needs one. A browser therefore keeps
its watchlist across reloads and server restarts, but not across
browsers or after clearing cookies.

Neither is authentication: anyone can send any X-User-Id. Everything
that needs the user goes through current_user_id(), so swapping this
for the Symphony identity later is a change to this one function.

    @app.get("/watchlists/me")
    async def my_watchlist(user_id: str = Depends(current_user_id)): ...
"""

import re
import uuid

from fastapi import HTTPException, Request, Response

USER_HEADER = "X-User-Id"
USER_COOKIE = "mi_user"
COOKIE_MAX_AGE = 400 * 24 * 3600  # the longest browsers keep a cookie

_VALID_ID = re.compile(r"[A-Za-z0-9._@:-]{1,128}")


def current_user_id(request: Request, response: Response) -> str:
    """The requesting user's ID. Issues a new anonymous one, in a cookie
    on `response`, if the request carries none. 400 for a malformed
    X-User-Id; a malformed cookie is replaced."""
    header = request.headers.get(USER_HEADER)
    if header is not None:
        if not _VALID_ID.fullmatch(header):
            raise HTTPException(
                status_code=400,
                detail=f"{USER_HEADER} must be 1-128 letters, digits or . _ @ : -",
            )
        return header
    cookie = request.cookies.get(USER_COOKIE)
    if cookie and _VALID_ID.fullmatch(cookie):
        return cookie
    user_id = "anon-" + uuid.uuid4().hex
    response.set_cookie(
        USER_COOKIE, user_id, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return user_id
