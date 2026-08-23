# Gateway `Mapping() takes no arguments` — investigation notes

**Status:** unresolved, not reproduced. Single occurrence.
**Date:** 2026-08-23

---

## Corrections to earlier claims

Two things I reported earlier were wrong and are corrected here:

1. **Frequency: 1 occurrence in 30 days, not 3.** The earlier count came from
   `grep -c` matching three *lines* emitted by a single event (the ERROR line,
   the bare `TypeError:` line in the traceback, and the WARNING summary).
2. **No retry or fallback is visible.** The log records `attempt 1/3` and then
   nothing. There are **zero** `Fallback activated` lines in 30 days of journal,
   and `MiniMax-M3` appears exactly once in that entire day — the failure itself.

---

## The event

```
2026-08-22T17:53:23-04:00
ERROR agent.chat_completion_helpers: Streaming failed before delivery:
      Mapping() takes no arguments
WARNING agent.conversation_loop: API call failed (attempt 1/3)
  error_type=TypeError
  thread=hermes-gateway_0:140588347279040
  provider=minimax-oauth
  base_url=https://api.minimax.io/anthropic
  model=MiniMax-M3
```

Call path:

```
agent/chat_completion_helpers.py:4543  _call_anthropic
agent/relay_llm.py:395                 stream
agent/chat_completion_helpers.py:4512  _open_anthropic_stream
  -> request_client.messages.stream(**final_kwargs)
anthropic/resources/messages/messages.py:1111  maybe_transform
anthropic/_utils/_transform.py:88   maybe_transform
anthropic/_utils/_transform.py:280  _transform_typeddict
anthropic/_utils/_transform.py:179  if is_typeddict(...) and is_mapping(data)
anthropic/_utils/_utils.py:160      return isinstance(obj, Mapping)
typing.py:1328   __instancecheck__
typing.py:1606   __subclasscheck__
<frozen abc>:123 __subclasscheck__
TypeError: Mapping() takes no arguments
```

Environment: anthropic 0.87.0, pydantic 2.13.4, typing_extensions 4.15.0,
CPython 3.11.15 (bundled `.hermes-runtime` build), Linux, multithreaded gateway.

---

## What the error text actually means

The message is `object.__init__`'s complaint when a class defining no custom
`__init__`/`__new__` is **called** with arguments. Verified locally:

```
typing.Mapping()      -> TypeError: Can't instantiate abstract class Mapping
                                    with abstract methods __getitem__, __iter__, __len__
typing.Mapping(1)     -> TypeError: Mapping() takes no arguments
typing.Mapping({})    -> TypeError: Mapping() takes no arguments
typing.Mapping(a=1)   -> TypeError: Mapping() takes no arguments
```

**Only the argument-bearing call produces our exact text.** A bare
instantiation attempt gives a different message.

That is the sharpest fact available: somewhere in that stack, `Mapping` is
being *called with an argument*, not merely used in an `isinstance` check.
Which is strange, because `isinstance`/`issubclass` should never call the
class.

---

## What was ruled out

**Thread-safety race in ABC caching.** Two reproduction attempts, both clean:

- 32 threads × 400 never-before-checked classes, released simultaneously from a
  barrier → 0 errors.
- 16 threads running `isinstance(x, typing.Mapping)` on freshly created classes
  while 8 threads called `collections.abc.Mapping.register()` to force global
  ABC cache invalidation, 30 seconds → 0 errors.

**A competing class named `Mapping`.** Only two distinct `Mapping` objects are
reachable in-process: `typing.Mapping` (shared by the SDK modules and
`typing_extensions`) and `collections.abc.Mapping`. No third-party shadow.

**A direct call in our code or the SDK's transform path.** `grep` finds no
`Mapping(...)` call site in `anthropic/_utils/_transform.py`,
`anthropic/_utils/_utils.py`, or in Hermes `agent/`, `gateway/`, `tools/`
(the only matches are an unrelated `TokenMapping(...)` constructor).

**Pathological objects.** Objects with a hostile metaclass, a
`__subclasshook__` returning `NotImplemented`, and a `__class__` property
returning the alias itself all pass `isinstance(obj, typing.Mapping)` without
error.

---

## What remains unknown

- **Why the call has an argument.** The most plausible remaining explanation is
  that *some value in the payload* has a type whose subclass check re-enters
  and constructs something. A user-defined `Mapping` subclass in the payload
  reproduces the identical text when constructed — that is the lead worth
  pulling.
- **Whether the request recovered.** `attempt 1/3` with no subsequent line
  could mean a silent retry succeeded without logging, or the turn was
  abandoned. The absence of any `Fallback activated` line in 30 days suggests
  the fallback chain may not be exercising at all.
- **Whether it is a CPython, SDK, or Hermes bug.** A dedicated research pass
  (CPython tracker, anthropic-sdk-python tracker, Stack Overflow, general web)
  found **no public report** of this error arising from
  `isinstance(obj, typing.Mapping)`. Conclusion returned: `not_found`,
  confidence low. Nothing to pin, patch against, or wait on upstream.

---

## Do not

**Do not "fix" this by removing MiniMax from `fallback_providers`.** That hides
a fault in the failover path — the path that only runs when something else has
already gone wrong. If the fallback chain is silently not engaging, that is a
second and more serious finding than the `TypeError` itself.

---

## Next

1. Determine whether `fallback_providers` engages at all. Zero `Fallback
   activated` lines in 30 days is either "the primary never failed hard enough"
   or "failover is broken". Establish which.
2. If reproduction is wanted, instrument `_open_anthropic_stream` to log
   `type()` and `repr()` of each `final_kwargs` value on exception, then wait
   for a natural recurrence rather than forcing one.
3. Given a rate of once per 30 days with no user-visible impact recorded,
   treat this as **observe-and-instrument**, not a blocking defect.
