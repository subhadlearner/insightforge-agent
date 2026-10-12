"""Persona selection (design.md section 9). This is identity selection for the demo, NOT
authentication: anyone who can reach the API can pick any persona. The cookie is signed only so
that a request cannot claim a persona it never selected by editing the cookie.

The session cookie holds the persona ID and an HMAC-SHA256 of it under the app's session secret,
and is HttpOnly. `PersonaSession.current_user` is the one FastAPI dependency that turns it into a
`User`: a missing, malformed or tampered cookie, or one naming a persona that is no longer in
users.yml, is 401 and never another persona."""

import base64
import hashlib
import hmac
from collections.abc import Sequence

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel

from insightforge_agent.domain.models import User

COOKIE_NAME = "insightforge_persona"
MIN_SECRET_CHARS = 32
_PURPOSE = b"insightforge-persona-session\x1f"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PersonaOut(BaseModel):
    id: str
    display_name: str
    source_toggles: dict[str, bool]


class SelectIn(BaseModel):
    persona_id: str


class PersonaSession:
    """Signs and verifies persona cookies, and resolves them against the configured personas."""

    def __init__(self, personas: Sequence[User], secret: str, *, secure: bool = False) -> None:
        if len(secret) < MIN_SECRET_CHARS:
            raise ValueError(f"SESSION_SECRET must have at least {MIN_SECRET_CHARS} characters")
        self._by_id = {u.id: u for u in personas}
        self._key = secret.encode()
        self._secure = secure

    @property
    def personas(self) -> list[User]:
        return list(self._by_id.values())

    def _signature(self, persona_id: str) -> bytes:
        return hmac.new(self._key, _PURPOSE + persona_id.encode(), hashlib.sha256).digest()

    def sign(self, persona_id: str) -> str:
        return f"{_b64(persona_id.encode())}.{_b64(self._signature(persona_id))}"

    def verify(self, value: str | None) -> User | None:
        """The persona a cookie value names, or None if it is not one this app signed for a
        persona that still exists."""
        if not value or value.count(".") != 1:
            return None
        encoded_id, encoded_sig = value.split(".")
        try:
            persona_id = _unb64(encoded_id).decode()
            signature = _unb64(encoded_sig)
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(signature, self._signature(persona_id)):
            return None
        return self._by_id.get(persona_id)

    def current_user(self):
        """The FastAPI dependency every owner-scoped endpoint uses."""
        def dependency(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> User:
            user = self.verify(session)
            if user is None:
                raise HTTPException(401, "select a persona first")
            return user
        return dependency

    def current_user_id(self):
        current = self.current_user()

        def dependency(user: User = Depends(current)) -> str:
            return user.id
        return dependency

    def router(self) -> APIRouter:
        router = APIRouter()
        current = self.current_user()

        def out(u: User) -> PersonaOut:
            return PersonaOut(id=u.id, display_name=u.display_name,
                              source_toggles=u.source_toggles)

        @router.get("/personas", response_model=list[PersonaOut])
        def list_personas():
            return [out(u) for u in self.personas]

        @router.post("/session", response_model=PersonaOut)
        def select(body: SelectIn, response: Response):
            user = self._by_id.get(body.persona_id)
            if user is None:
                raise HTTPException(422, f"unknown persona {body.persona_id!r}")
            response.set_cookie(COOKIE_NAME, self.sign(user.id), httponly=True,
                                samesite="lax", secure=self._secure, path="/")
            return out(user)

        @router.get("/session", response_model=PersonaOut)
        def who(user: User = Depends(current)):
            return out(user)

        @router.delete("/session", status_code=204)
        def leave(response: Response):
            response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax",
                                   secure=self._secure)

        return router
