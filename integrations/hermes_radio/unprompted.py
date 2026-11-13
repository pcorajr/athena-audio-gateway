"""Loopback threat reservations, shared with ordinary replies through Eye's announced keys.

No background sender here. The gateway owns radio, priority, audio and delivery.
A reservation lasts 45 seconds (longer than the gateway's bounded playback), and
an explicit failure or a failed journal outcome re-arms it. Mission changes fence
both the last-heard route and outstanding reservations.
"""
from datetime import datetime
import os
import re
import threading
import time

from .contract import ContractError, safe_text, strict_json, response


class Unprompted:
    def __init__(self, controller, *, clock=time.monotonic):
        self.controller, self.clock = controller, clock
        self.lock = threading.RLock()
        self.heard = None
        self.claims = {}
        self.last = -float('inf')

    def marker(self):
        eye = self.controller.eye
        return (getattr(eye, 'reset_at', None), getattr(eye, 'session_id', None))

    @staticmethod
    def enabled():
        return os.environ.get('ATHENA_UNPROMPTED', 'on').lower() == 'on'

    def remember(self, request):
        with self.lock:
            eye = self.controller.eye
            if eye is None or len(request.transcript.strip(' -.')) < 2:
                return False
            received = datetime.fromisoformat(request.received_at.replace('Z', '+00:00')).timestamp()
            if received < (getattr(eye, 'reset_at', 0) or 0):
                return False
            if self.heard and received < self.heard[2]:
                return False
            self.heard = (request, self.marker(), received)
            return True

    @staticmethod
    def current(pic):
        cp, own = pic.get('cockpit') or {}, pic.get('ownship') or {}
        age = (pic.get('data_age_s') or {}).get('units')
        return bool(own and pic.get('connected') and isinstance(age, (float, int)) and 0 <= age <= 10
                    and not (cp.get('fresh') and cp.get('aircraft') and own.get('type')
                             and cp['aircraft'] != own['type']))

    def _finish(self, ident, outcome):
        claim = self.claims.pop(ident, None)
        if claim and claim['marker'] == self.marker() and outcome != 'sent':
            self.controller.eye.unmark_announced(claim['keys'])
        return claim is not None

    def poll(self, ident, reply_to):
        c = self.controller
        # Never race a picture/mark operation in the ordinary reply. In-flight
        # playback does not hold this lock; a launch can interrupt it immediately.
        if not c._turn.acquire(blocking=False):
            return None
        try:
            with self.lock:
                if not self.enabled() or not self.heard or self.heard[1] != self.marker():
                    return None
                request = self.heard[0]
                if request.transmission_id != reply_to:
                    return None
                for key, claim in list(self.claims.items()):
                    outcome = c.transcript.outcome_of(key)
                    if outcome is not None or claim['marker'] != self.marker() or self.clock() - claim['at'] > 45:
                        self._finish(key, outcome or 'expired')
                if any(claim['priority'] == 'launch' for claim in self.claims.values()):
                    return None
                with c.eye.lock:
                    marker = self.marker()
                    pic = c.eye.picture()
                    if not self.current(pic):
                        return None
                    rows = list(zip(pic.get('rwr_alert_to_announce') or [], pic.get('_rwr_keys') or []))
                    allowed = [(call, key, 'launch' if call.startswith('Missile launch,') else 'threat')
                               for call, key in rows if call.startswith(('Missile launch,', 'Caution,'))
                               or (' tracking you,' in call and call.startswith('SAM '))]
                    if pic.get('_own_loss_key'):
                        allowed.append(("Prometheus, Athena, you're down.", pic['_own_loss_key'], 'threat'))
                    if not allowed:
                        return None
                    call, key, priority = min(allowed, key=lambda row: row[2] != 'launch')
                    if self.claims and priority != 'launch':
                        return None  # a new launch may jump an already queued caution
                    if priority != 'launch' and self.clock() - self.last < 5:
                        return None
                    keys = [key]
                    # Up to two same-type launches share one short call. Different
                    # types retain their own designation in separate short calls.
                    if priority == 'launch':
                        call = call.replace('Missile launch, ', '').replace(', your ', ', ')
                        call = re.sub(r'^([^,]+),', r'\1 launch,', call)
                        launches = [(a, k) for a, k, p in allowed if p == 'launch'][:2]
                        parsed = [re.fullmatch(r"Missile launch, ([^,]+), your (.+) o'clock, .+\.", a) for a, _ in launches]
                        if len(parsed) == 2 and parsed[0] is not None and parsed[1] is not None and parsed[0][1] == parsed[1][1]:
                            call = "Two %s missiles, %s and %s o'clock." % (parsed[0][1], parsed[0][2], parsed[1][2])
                            keys = [k for _, k in launches]
                    if len(call) > 280:
                        return None
                    c.eye.mark_announced(keys)
                    self.last = self.clock()
                    self.claims[ident] = dict(keys=keys, marker=marker, at=self.clock(), request=request, speech=call, priority=priority)
                    frame = response(request, speech=call, state='ok', route='unprompted', priority=priority)
                    frame['transmission_id'] = ident
                    c.transcript.log(kind='unprompted_generated', request=request, text=call, route='unprompted',
                                     outcome='reserved', alert_id=ident)
                    return frame
        finally:
            c._turn.release()

    def receipt(self, ident, outcome):
        c = self.controller
        with self.lock:
            claim = self.claims.get(ident)
            if claim:
                if outcome == 'check':
                    return bool(self.enabled() and claim['marker'] == self.marker()
                                and self.clock() - claim['at'] < 5 and self.heard
                                and self.heard[0].transmission_id == claim['request'].transmission_id
                                and self.current(c.eye.picture()))
                if outcome == 'sent' and claim['marker'] == self.marker():
                    self.last = self.clock()  # floor from actual completion, not reservation/TTS start
                    if c._turn.acquire(blocking=False):
                        try:
                            c._last_delivered = ('', claim['speech'], time.monotonic())
                        finally:
                            c._turn.release()
                return self._finish(ident, outcome)
        if outcome != 'check' and c._turn.acquire(blocking=False):
            try:
                if c._last_exchange and c._last_exchange['id'] == ident:
                    c._settle_previous(None, reported=outcome)
                    return True
            finally:
                c._turn.release()
        return False


def control_request(raw, action):
    value = strict_json(raw)
    keys = {'transmission_id', 'reply_to'} if action == 'poll' else {'transmission_id', 'outcome'}
    if type(value) is not dict or set(value) != keys:
        raise ContractError()
    for text in value.values():
        if len(safe_text(text)) > 256:
            raise ContractError()
    if action != 'poll' and value['outcome'] not in {'check', 'sent', 'failed', 'interrupted', 'superseded'}:
        raise ContractError()
    return value
