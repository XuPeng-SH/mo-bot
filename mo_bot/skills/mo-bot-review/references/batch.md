## Two-PR batch assignments

Only an explicit coordinator batch assignment authorizes two PRs in one turn.
Each member retains its own frozen checkout/head, discussion ledger, review plan,
validation and verdict. Use explicit paths and identify the PR in commentary.
Read identical repository instructions/background once after comparing contents
or hashes; inspect differences and applicable references separately. Evidence and
correctness conclusions remain bound to the source and contract they establish.

Finish one member before proceeding to the other unless useful work is blocked.
After completing its ledger and validation, atomically write its ordinary
assessment JSON to `review-result.json` in that member's diagnostic directory
(write a temporary file, then rename). This is a final checkpoint that the
coordinator may publish immediately; leave that assessment and its evidence fixed.
Use each member's `check-run`/`test-exec` paths to keep its execution receipts local.
Return only `{"completed_job_ids":[first_job_id,second_job_id]}` as the final
JSON acknowledgement. The assessment files are the results; do not repeat their
bodies in the final response. The coordinator handles independent
publication, changed inputs and recovery. Ordinary assignments retain the single
assessment format.

