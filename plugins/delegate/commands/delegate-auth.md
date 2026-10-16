---
description: Check which worker CLIs and accounts are signed in, and guide sign-in or add an account for more quota
argument-hint: "[cli] [account]"
---

Use the `orchestrate` skill (read `references/auth-and-fallback.md`).

Arguments: $ARGUMENTS

1. Call `delegate_auth` (for the named CLI if one is given; it opens the sign-in window of each one missing) and
   show a compact table: cli, account, signed in, detail. Tell the user which windows opened and to finish there.
2. For each account still not signed in without a window, show its exact login command. Offer
   `delegate_dashboard` for a page that shows and manages all of it.
3. If a CLI and account were named, or the user agrees, call `delegate_login(cli, account)` to open the CLI's own
   sign-in window, and tell the user to finish it there. A new account name creates a new account (more quota).
4. When the user says they are done, run `delegate_auth` again to confirm and clear stale rests with
   `delegate_limits(clear="<cli>@<account>")`.
5. Finish with `delegate_limits()` so the user sees any quota group still resting and until when.
6. Both agy quota groups resting: suggest another agy account, `delegate_login("agy", "<name>")` (file auth: its
   own home and token file, sign-in is a link to click). Only with `agy_file_auth` off, or when the user asks for
   `agy-switch`: explain that agy's current Google account will be signed out;
   only on the user's yes call `delegate_agy_switch(confirm_logout=true)`, give them the returned `url`, and pass
   the response code they paste back to `delegate_agy_code`. Report the new email.
