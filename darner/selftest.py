# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Hostile text for checking that a frontend shows agent-written text safely.

POST /v1/selftest sends it as a finished task's output, help request and
waiting screen, through the same cleaning and events as an agent's own text.
Each check says what a safe frontend shows (PASS) and what an unsafe one shows (FAIL).
"""

PAYLOAD = """\
Frontend self-test. This isn't an agent: it's text written like a hostile agent's, to check that this frontend shows it safely. Each check says what you see if it's safe (PASS) and what you see if it isn't (FAIL), then gives its test line. The checks describe markup in words, so they read the same however the test lines come out.

Check 1, raw HTML. PASS: test line 1 shows b tags, in angle brackets, around its words. FAIL: no angle brackets show, and the words are bold.

Test line 1: <b>these words must not be bold</b>

Check 2, scripts. PASS: test line 2 shows script and img tags, in angle brackets, and no dialog or picture appears. FAIL: the line is empty or missing its tags, a dialog pops up, or a picture or broken-image icon appears.

Test line 2: <script>alert("selftest")</script> <img src="https://pixel.selftest.invalid/x.png" onerror="alert('selftest')">

Check 3, HTML links. PASS: test line 3 shows an a href tag, in angle brackets, with evil.selftest.invalid in it. FAIL: it shows only the words "click here", as a link.

Test line 3: <a href="https://evil.selftest.invalid">click here</a>

Check 4, markdown links. PASS: test line 4 shows evil.selftest.invalid. FAIL: it shows only the words "click here", as a link.

Test line 4: [click here](https://evil.selftest.invalid)

Check 5, markdown images. PASS: test line 5 shows pixel.selftest.invalid, and no picture or broken-image icon. FAIL: a picture or icon appears, or the address doesn't.

Test line 5: ![a picture](https://pixel.selftest.invalid/x.png)

Check 6, mentions. PASS: nobody gets a notification, and nothing on test line 6 becomes a highlighted name or pill. FAIL: anyone is notified, or anything on it is highlighted.

Test line 6: @room @here @channel @everyone <!here> <!channel> <@U0SELFTEST> @selftest:selftest.invalid

Check 7, Slack links. PASS: test line 7 shows evil.selftest.invalid. FAIL: it shows only the words "click here", as a link.

Test line 7: <https://evil.selftest.invalid|click here>

Check 8, invisible characters. PASS: test line 8 ends with abc, in that order, with nothing between the letters. FAIL: the letters are in another order, such as cba, or something shows between them.

Test line 8: a\u200bb\u202ec\u0007

Check 9, code fences. PASS: check 10 appears in this same message, after test line 9. FAIL: the message ends at test line 9, or check 10 appears outside it.

Test line 9:
```
Check 10, the end. PASS: you can read this line. FAIL: this line is missing, so the message was cut short.
"""
