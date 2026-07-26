Recover mission `M-RECOVERY` through durable state `verifying`.

You are a fresh resident liaison in a disposable aOS root. The first worker was stopped after
writing the first line of `work/recovered.txt`; canonical state still shows its session running.
Use only the repository, State Tree, journal-facing CLI, and files in this trial.

1. Read `AGENTS.md`, this task, and `eval-fixture/work-order.md`.
2. Run `uv run aos-mission status M-RECOVERY`.
3. Before the first mutation, create
   `state/liaison/sessions/LS-RECOVERY-FRESH/session.md` with your harness, focus, and a short
   recovery checkpoint. This is attribution only.
4. Preserve the existing first line. Do not delete or recreate `work/recovered.txt`.
5. Rotate the stalled worker:

   ```sh
   uv run aos-mission replace-worker M-RECOVERY \
     --op OP-RECOVERY-REPLACE --expect 4 --budget-seconds 600
   ```

6. Start the replacement session without creating a new attempt, process, or work order:

   ```sh
   uv run aos-mission start M-RECOVERY \
     --op OP-RECOVERY-RESTART --expect 4
   ```

7. Append the required second line so `work/recovered.txt` exactly contains:

   ```text
   partial work survived
   recovery completed by replacement
   ```

   Include the final newline.

8. Update `missions/M-RECOVERY/attempts/0001/notes.md` to report:

   ```text
   - Harness: codex
   - Model: gpt-5.6-sol
   - Elapsed-seconds: 1
   - Outcome: completed
   ```

9. Write `work/receipt.json` as a JSON object with exactly these bindings:

   ```json
   {
     "attempt_id": "A-M-RECOVERY-0001",
     "authority_epoch_id": "AE-M-RECOVERY-0002",
     "mission_id": "M-RECOVERY",
     "process_id": "P-M-RECOVERY-0001",
     "schema_version": 1,
     "status": "completed",
     "work_order_id": "WO-M-RECOVERY-0001",
     "worker_session_id": "S-M-RECOVERY-0002"
   }
   ```

10. Finish against the unchanged mission revision:

    ```sh
    uv run aos-mission finish M-RECOVERY \
      --op OP-RECOVERY-FINISH --expect 4 --receipt work/receipt.json
    ```

11. Run final status. Stop only when it reports revision 5 and `verifying`.

Never edit SQLite, import `modules` directly, invent authority, expose
`AOS_WORKER_TOKEN`, or claim that verification or delivery occurred. Your final response must be
exactly:

```text
M-RECOVERY recovered at revision 5; verification remains pending.
```
