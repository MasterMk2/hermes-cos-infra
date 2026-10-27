#!/usr/bin/env python3
"""Unified Channel Gateway — coding handoff orchestration (Phase J.1).

`gw_coding` decides *what a turn is*; this module carries that decision out.
It is the only place where the three pieces meet: the pure classifier, the
durable proposal row, and the typed broker.

Everything it can return is a string to send back to the owner, or `None`
meaning "not a coding turn — send it to Hermes exactly as before". There is no
third outcome, and in particular there is no path where the route swallows a
turn silently.

Ordering matters in `handle`, and the order is: authority first, then state,
then the network. A turn from somebody who is not the owner never reaches the
broker, and a broker that is down can never turn into a claimed job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import gw_coding as coding
import gw_store as store
from gw_handoff_client import OK as CLIENT_OK

log = logging.getLogger("channel-gateway.handoff")

#: Broker refusal codes this module reacts to by name. Declared here so the two
#: sides cannot drift apart without a test noticing.
SCOPE_CHANGED = "scope_changed_since_proposal"
SPEC_CHANGED = "spec_changed_since_proposal"


class CodingHandoff:
    """Owns the coding route for one running gateway."""

    #: How long a failed registry lookup suppresses the next one. A broker that
    #: is down must not be re-dialled on every turn, and the owner must not pay
    #: its timeout repeatedly to be told the same thing.
    FAILURE_BACKOFF_SECONDS = 30

    def __init__(self, *, client, in_thread, ttl_seconds: int,
                 projects_cache_seconds: int = 120):
        self.client = client
        self._in_thread = in_thread
        self.ttl_seconds = int(ttl_seconds)
        self._cache_seconds = int(projects_cache_seconds)
        self._projects = ()
        self._projects_at = 0.0
        self._have_projects = False
        self._failed_until = 0.0

    # -- registry -----------------------------------------------------

    async def _known_projects(self) -> tuple:
        """`(projects, usable)` — the enabled project names, cached briefly.

        `usable` is the important half. An empty tuple because the operator has
        registered nothing and an empty tuple because the broker is unreachable
        look identical, and treating the second as the first would tell the
        owner their project is "not registered" during an outage — which is
        both false and the kind of answer that sends someone to change a
        registry that was never wrong.

        A stale cache is safe in the direction that matters: it can only fail
        to recognise a project the operator just added, and the broker resolves
        the key again on every call, so it can never invent one.
        """
        now = time.monotonic()
        if self._have_projects and (now - self._projects_at) < self._cache_seconds:
            return self._projects, True
        if now < self._failed_until:
            return self._projects, self._have_projects
        payload, status = await asyncio.to_thread(self.client.projects)
        if status != CLIENT_OK or not isinstance(payload, dict):
            self._failed_until = now + self.FAILURE_BACKOFF_SECONDS
            log.warning("coding handoff registry unavailable (%s)", status)
            return self._projects, self._have_projects
        refs = []
        for entry in payload.get("projects") or []:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            refs.append(coding.ProjectRef(
                key=str(entry["key"]),
                aliases=tuple(str(a) for a in (entry.get("aliases") or [])),
            ))
        self._projects = tuple(refs)
        self._projects_at = now
        self._have_projects = True
        self._failed_until = 0.0
        return self._projects, True

    # -- proposal state -----------------------------------------------

    async def _proposal(self, conversation_id: str, request_id: str):
        row = await self._in_thread(store.get_proposal, conversation_id)
        if row is None:
            return None, None
        # Consumed *by another message*. A redelivery of the message that spent
        # the proposal must still see it as usable, because its answer may
        # never have reached the owner — the broker's ledger is what stops that
        # from becoming a second job.
        consumed_elsewhere = bool(row["consumed_at"]) and \
            row["consumed_by"] != request_id
        return coding.Proposal(
            conversation_id=row["conversation_id"],
            project_key=row["project_key"],
            token=row["token"],
            created_at=row["created_at"],
            origin_request_id=row["origin_request_id"],
            scope_digest=row["scope_digest"] or "",
            spec_revision=int(row["spec_revision"] or 0),
            spec_sha256=row["spec_sha256"] or "",
            consumed=consumed_elsewhere,
            pending_authorization_id=(row["pending_authorization_id"] or ""),
        ), row

    # -- entry point --------------------------------------------------

    async def handle(self, message) -> str | None:
        # Local state first, network last. Reading one indexed row from the
        # gateway's own database is cheap; a broker round trip is not, and an
        # ordinary turn about tomorrow's schedule must never wait on one.
        proposal, row = await self._proposal(message.conversation_id,
                                             message.gateway_request_id)
        if not coding.is_plausible_coding_turn(
                message, has_context=proposal is not None):
            return None

        history = []
        if proposal is None:
            history = await self._in_thread(store.recent_owner_inputs, message)
            if coding.is_short_followup(message.text) and not history:
                return None

        projects, usable = await self._known_projects()
        if not usable:
            if proposal is None and coding.is_short_followup(message.text):
                # History alone does not prove this ordinary short turn is
                # about coding when the registry cannot resolve its target.
                return None
            # The broker could not be reached. Saying so is right; classifying
            # against an empty registry would report every project unregistered.
            log.info("coding route unavailable for %s", message.gateway_request_id)
            return (coding.render_outcome_unknown(proposal.project_key)
                    if proposal and proposal.pending_authorization_id
                    else coding.render_unavailable())

        decision = coding.classify(
            message, enabled=True, projects=projects, proposal=proposal,
            context_project_key=coding.project_from_owner_history(projects, history),
            now=datetime.now(timezone.utc), ttl_seconds=self.ttl_seconds,
        )
        if not decision.handled:
            return None

        log.info("coding route %s request=%s project=%s reason=%s",
                 decision.action, message.gateway_request_id,
                 decision.project_key or "-", decision.reason)

        if decision.action == coding.UNKNOWN_PROJECT:
            return coding.render_unknown_project(decision.named)
        if decision.action == coding.NEEDS_EXPLICIT:
            if decision.reason == "pending_authorization_unresolved":
                return (f"{decision.project_key} の実行結果がまだ未確定です。"
                        "先に「進捗」で確認するか、元の指示を再試行してください。"
                        "別のプロジェクトの提案への差し替えは行っていません。")
            if decision.reason == coding.ALREADY_CONSUMED:
                return coding.render_already_consumed()
            token = row["token"] if row is not None else ""
            return coding.render_needs_explicit(
                decision.project_key or "", token, decision.reason)
        if decision.action == coding.REQUEST:
            return await self._propose(message, decision.project_key)
        if decision.action == coding.AUTHORIZE:
            return await self._authorize(message, proposal, decision.project_key)
        if decision.action == coding.STATUS:
            return await self._status(message, decision)
        return None

    # -- actions ------------------------------------------------------

    async def _propose(self, message, project_key: str) -> str:
        selected = await self._in_thread(store.select_proposal_project,
                                         message.conversation_id, project_key)
        if not selected:
            return "前の実行結果が未確定のため、提案を差し替えられません。「進捗」で確認してください。"
        payload, status = await asyncio.to_thread(self.client.preflight, project_key)
        if status != CLIENT_OK:
            return coding.render_unavailable()
        if payload.get("error") == "project_not_registered":
            return coding.render_unknown_project(project_key)
        if payload.get("error"):
            return coding.render_not_ready(
                project_key, [{"code": payload["error"], "detail": ""}])
        if not payload.get("ready"):
            # Registered, but something real is in the way. Nothing is created,
            # and the owner is told exactly what is missing.
            return coding.render_not_ready(project_key, payload.get("missing"))

        digest = str(payload.get("scope_digest") or "")
        spec = payload.get("spec") or {}
        if not digest:
            return coding.render_unavailable("承認対象を特定できませんでした")

        token = store.new_proposal_token()
        stored = await self._in_thread(
            store.put_proposal,
            conversation_id=message.conversation_id,
            project_key=str(payload.get("project_key") or project_key),
            token=token,
            scope_digest=digest,
            origin_request_id=message.gateway_request_id,
            spec_revision=int(spec.get("revision") or 0),
            spec_sha256=str(spec.get("sha256") or ""),
        )
        if not stored:
            return "前の実行結果が未確定のため、提案を差し替えられません。「進捗」で確認してください。"
        return coding.render_scope(payload.get("scope") or {}, token, spec)

    async def _authorize(self, message, proposal, project_key: str) -> str:
        # Claim the proposal before anything leaves this process. A second
        # 「実行して」 arriving while the first is still in the broker must not
        # produce a second job, and the claim is what makes that true even
        # though the two turns are handled concurrently.
        pending = (proposal.pending_authorization_id if proposal else "") or ""
        if pending:
            # A previous attempt's outcome was never learned. Retrying under
            # the *original* id is what keeps one approval to one job: the
            # broker's ledger recognises it and returns the job it already
            # made, if it made one.
            authorization_id = pending
            log.info("retrying pending authorization %s as %s",
                     authorization_id, message.gateway_request_id)
        else:
            claimed = await self._in_thread(
                store.consume_proposal, message.conversation_id,
                message.gateway_request_id,
                project_key=project_key, scope_digest=proposal.scope_digest,
                mark_pending=True)
            if not claimed:
                return ("この確認はすでに処理済みです。"
                        "最新の状況は「進捗」と聞いてください。")
            authorization_id = message.gateway_request_id

        # Recorded before the request leaves. If the reply never arrives
        # this row is the only thing tying a later turn back to the
        # authorization that may already have created a job.
        payload, status = await asyncio.to_thread(
            self.client.authorize,
            authorization_id=authorization_id,
            conversation_id=message.conversation_id,
            project_key=project_key,
            scope_digest=proposal.scope_digest if proposal else "",
        )
        if status != CLIENT_OK:
            # The authorization was sent. Whether the broker acted on it before
            # the connection failed is unknown from here, and saying "no job
            # was created" would be a claim about production state this side
            # cannot support. The ledger records the derived job id before the
            # job exists, so a status question resolves it.
            log.warning("coding authorize outcome unknown authorization=%s "
                        "status=%s", authorization_id, status)
            return coding.render_outcome_unknown(project_key)

        error = payload.get("error")
        if error == "authorization_in_progress":
            return coding.render_in_progress(payload.get("job_id"))
        if not error and (not payload.get("job_id") or not payload.get("status")):
            return coding.render_outcome_unknown(project_key)
        if payload.get("status") in ("NEW", "FAILED_TO_START"):
            return (coding.render_status(payload) +
                    "\n起動は保留されています。条件の復旧後に「実行して」で同じ承認を再試行できます。")
        # Only a definitive result may release this immutable approval binding.
        await self._in_thread(store.clear_authorization_pending,
                              message.conversation_id, authorization_id)
        if error and payload.get("job_id"):
            return (coding.render_status(payload) + "\n起動は保留されています。" +
                    str(payload.get("detail") or "承認済みの実行条件を再確認してください。"))
        if error in (SCOPE_CHANGED, SPEC_CHANGED):
            return coding.render_scope_changed(project_key)
        if error == "authorization_conflict":
            return coding.render_conflict(project_key)
        if error == "not_ready":
            return coding.render_not_ready(project_key, payload.get("missing"))
        if error:
            return coding.render_not_ready(
                project_key, [{"code": str(error), "detail":
                               str(payload.get("detail") or "")}])
        return coding.render_authorized(payload)

    async def _status(self, message, decision) -> str:
        payload, status = await asyncio.to_thread(
            self.client.status,
            job_id=decision.job_id or "",
            project_key=decision.project_key or "",
            authorization_id=decision.authorization_id or "")
        if status != CLIENT_OK:
            return (coding.render_outcome_unknown(decision.project_key or "")
                    if decision.authorization_id else coding.render_unavailable())
        error = payload.get("error")
        if error == "job_not_from_handoff":
            if decision.project_key and not decision.job_id and not decision.authorization_id:
                # A project with no handoff job is not a permissions error.
                # Read the actual prerequisite status instead of returning the
                # conversation to an old read-only model refusal.
                preflight, pre_status = await asyncio.to_thread(
                    self.client.preflight, decision.project_key)
                if pre_status != CLIENT_OK:
                    return coding.render_unavailable()
                if not preflight.get("ready"):
                    return coding.render_not_ready(decision.project_key,
                                                   preflight.get("missing"))
                return (f"{decision.project_key} は実行準備ができています。"
                        "この受付から開始したジョブはまだありません。"
                        f"「{decision.project_key} の実装を始めて」で実行範囲を確認できます。")
            return ("そのジョブはこのチャネルから開始したものではないため、"
                    "ここでは状況を確認できません。")
        if decision.authorization_id:
            # This turn is about one specific approval, so the answer has
            # to be about that approval — never the project's most recent
            # job, which could be unrelated work from weeks ago.
            if error == "authorization_unknown":
                return coding.render_no_such_authorization(
                    decision.project_key or "")
            if error:
                return coding.render_outcome_unknown(decision.project_key or "")
            if not payload.get("job_id") or not payload.get("status"):
                return coding.render_authorization_uncertain(
                    decision.project_key or "", payload.get("job_id"))
            if payload.get("status") not in ("NEW", "FAILED_TO_START"):
                await self._in_thread(store.clear_authorization_pending,
                                      message.conversation_id, decision.authorization_id)
        if error:
            return coding.render_status({})
        return coding.render_status(payload)
