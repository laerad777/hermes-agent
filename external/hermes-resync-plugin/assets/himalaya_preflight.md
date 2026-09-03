# Himalaya duplicate-send preflight

Perform this check immediately before sending mail. This is a manual safety
procedure; it does not send, modify, or delete any message.

1. Confirm the intended account, recipient set, subject, and attachments in the
   compose buffer. Do not rely on a saved draft's original recipients.
2. Search the **Sent** mailbox for the exact normalized subject and each primary
   recipient. Inspect candidates from the current delivery window, including
   messages sent from another client.
3. Compare the body and attachment names/hashes with any candidate. A matching
   recipient, subject, and materially identical content is a duplicate: **do not send**
   it. Reply to or forward the existing thread only when a new delivery is
   actually required.
4. Check the Outbox/queue for pending copies of the same message. Resolve a
   pending send before creating another copy.
5. Re-open the final compose preview and verify that only one send action will
   occur. If delivery status is uncertain, wait for Sent/Outbox evidence rather
   than retrying blindly.

Record no credentials or access tokens in the message, terminal history, or
preflight notes.
