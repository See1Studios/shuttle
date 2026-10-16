# Review checklist for shuttled work

Run through this for every work job before you land it. Write down (for yourself) the verdict of each section.

## A. Did it do the task?

- [ ] Every requirement in the brief's "Do" and "Done when" is met. Check them one by one against the diff.
- [ ] Nothing from "Don't" was violated.
- [ ] The report's claims match the diff (files listed = files changed; tests listed were actually relevant).
- [ ] Open questions in the report are answered or brought to the user.

## B. Scope and hygiene

- [ ] Only files the task needed are touched. No reformatting of untouched code, no unrelated renames.
- [ ] No debug prints, commented-out code, TODOs without owner, leftover scratch files, lockfile churn.
- [ ] No secrets, tokens, absolute personal paths, or machine-specific config committed.
- [ ] New files are in the right place and follow the repo's naming and layout.
- [ ] Repo-specific rules (AGENTS.md / CONTRIBUTING / code map / changelog) are respected.

## C. Correctness

- [ ] Read every hunk. For each changed function: inputs, edge cases (empty, None, errors, concurrency), return
      values, and every caller (`grep` / code index) still fit.
- [ ] Error handling is not swallowing failures; no silent fallbacks that hide bugs.
- [ ] Public interfaces, file formats, and persisted data stay compatible, or the break is intended and noted.
- [ ] Platform specifics (paths, encodings, line endings, shells) hold on the platforms the repo targets.

## D. Tests — verify, don't trust

- [ ] `cd <worktree>` and run the tests relevant to the change yourself. Record the command and result.
- [ ] New behaviour has a test, or there is a stated reason it can't.
- [ ] The tests would fail without the change (look at what they assert; a test that asserts nothing proves nothing).
- [ ] Linters / type checks / build pass where the repo has them.

## E. Risk

- [ ] Security: input validation, injection (shell, SQL, path), authz checks, unsafe deserialization.
- [ ] Performance: no accidental O(n²) on large inputs, no work in hot loops, no unbounded memory.
- [ ] For risky or large diffs, get a second opinion (`shuttle_review` with a different model family).

## Feedback format for `shuttle_followup`

```
1. path/to/file.py:42 — <what is wrong>. Expected: <what it should do>.
2. Tests: <which test is missing or failing, with the command to run it>.
3. Revert the unrelated change to path/other.py.
```

Concrete, numbered, one problem per line. No praise padding; the worker only needs what to change.
