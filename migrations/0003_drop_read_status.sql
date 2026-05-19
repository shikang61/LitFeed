-- Retired ``read`` status: the Read button always wrote ``saved``.
-- Normalize any legacy rows so counts and topics stay consistent.

UPDATE reading_log SET status = 'saved' WHERE status = 'read';
