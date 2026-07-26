# Mission protocol — E2E-00

The journal is canonical. The files under `missions/` are its inspectable State Tree. Every
contested transition must be admitted by `aos-v0`; shell activity never changes mission truth by
itself.

For `M-V0`, use this sequence and inspect the JSON after every command:

1. Read current state:

   ```sh
   uv run aos-v0 status
   ```

2. Record this resident harness session before the first mutation:

   ```sh
   mkdir -p state/liaison/sessions/LS-V0
   printf '%s\n' '# Liaison session LS-V0' '' 'Harness: Codex' 'Focus: M-V0' > state/liaison/sessions/LS-V0/session.md
   ```

   This authored record provides attribution and coordination only. It grants no authority and
   blocks no other session.

3. Relay the scripted human authorization against revision 1:

   ```sh
   uv run aos-v0 authorize M-V0 --op OP-V0-AUTHORIZE --expect 1 --decision decisions/authorize.json
   ```

4. Dispatch the authorized mission against revision 2:

   ```sh
   uv run aos-v0 dispatch M-V0 --op OP-V0-DISPATCH --expect 2
   ```

5. Start implementation against revision 3:

   ```sh
   uv run aos-v0 start M-V0 --op OP-V0-START --expect 3
   ```

6. Only after start succeeds, run the assigned deterministic worker once:

   ```sh
   sh eval-fixture/fake-worker.sh missions/M-V0/attempts/0001/worker-output
   ```

   Inspect `result.txt` and `receipt.json`. Do not alter either.

7. Relay the worker receipt and finish implementation against revision 4:

   ```sh
   uv run aos-v0 finish M-V0 --op OP-V0-FINISH --expect 4 --receipt missions/M-V0/attempts/0001/worker-output/receipt.json
   ```

8. Run the constitutional verifier against revision 5:

   ```sh
   uv run aos-v0 verify M-V0 --op OP-V0-VERIFY --expect 5
   ```

   Continue only if it reports successful evidence `EV-V0`.

9. Relay the scripted human approval against revision 6:

   ```sh
   uv run aos-v0 decide M-V0 --op OP-V0-DECIDE --expect 6 --decision decisions/approve.json
   ```

10. Confirm durable delivery:

    ```sh
    uv run aos-v0 status
    ```

The expected mission path is exactly:

```text
draft -> authorized -> dispatched -> implementing -> verifying
      -> awaiting_approval -> delivered
```

Authorization does not imply approval. Worker completion does not imply verifier success.
Verifier success does not imply delivery. Only the explicit approved decision may share the final
admitted operation with the transition to `delivered`.
