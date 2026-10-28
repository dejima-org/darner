# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Hostile text for checking that a frontend shows agent-written text safely.

POST /v1/selftest sends it as a finished task's output, help request and
waiting screen, through the same cleaning and events as an agent's own text.
Each numbered line says what a safe frontend shows.
"""

PAYLOAD = """\
Frontend self-test. This isn't an agent: it's text written to look like a hostile one. Each numbered line says what you should see. Anything else is a failure of the frontend that showed it.

1. Raw HTML shows as text, tags and all, with nothing bold or italic: <b>not bold</b> <i>not italic</i>
2. Script and event handlers show as text, and nothing runs or loads: <script>alert("selftest")</script> <img src="https://pixel.selftest.invalid/x.png" onerror="alert('selftest')">
3. An HTML link shows as text, not as a link: <a href="https://evil.selftest.invalid">https://good.selftest.invalid</a>
4. A markdown link shows that it goes to evil.selftest.invalid, whatever its text says: [https://good.selftest.invalid](https://evil.selftest.invalid)
5. A markdown image isn't loaded, and shows that it points at pixel.selftest.invalid: ![selftest](https://pixel.selftest.invalid/x.png)
6. None of these notifies anyone: @room @here @channel @everyone <!here> <!channel> <@U0SELFTEST> @selftest:selftest.invalid
7. Slack link syntax doesn't hide where it goes: evil.selftest.invalid shows, not just good: <https://evil.selftest.invalid|https://good.selftest.invalid>
8. Invisible characters are removed, so this reads "abc", with nothing between the letters: a\u200bb\u202ec\u0007
9. A code fence can't end this message's formatting early, so everything below it is still part of this message:
```
10. The last line. If you can read this, the whole self-test arrived.
"""
