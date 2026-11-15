### Fixed

- **Reading Meta's error object no longer raises, and the fields it reads carry no control character (#86).** A refused answer whose body nests deeper than the JSON parser follows raised `RecursionError`, and one whose code is infinite (`Infinity`, `1e999`) raised `OverflowError`, so a refused connect ended in an unhandled error instead of its refusal page, and a refused refresh raised before its rejection was recorded. Such a body now reads as one whose fields could not be read: the connect names its refusal, and the refresh records the rejection. A connect call's successful answer nested that deep is refused as malformed, as one that is not JSON already was. Provider text loses every control character before redaction, as it already lost NUL and the zero-width characters, so a type or a trace id holding a newline no longer prints one log record as two lines.

### Changed

- **One reader of Meta's error object serves the publish adapter, the past-posts read, and the connect and refresh log lines (#86).** `meta_adapter.error_object` reads the code and the subcode as an int or a string of decimal digits, never a bool, a float or a number of more than 18 digits, and every text field through the one provider-text redaction (`refusal_evidence.scrub_text`) with what the request carried, a type and a trace id bounded at 100 characters. Past-posts refusals now redact the error type and trace id, and read the code and subcode as the publish adapter reads them, so a code written as a string of digits routes and is recorded as that number. A code that is `true` or a float no longer reads as 1 or as its whole part. Rows already stored are unchanged.

### Tests

- **The tests hold the envelope Meta's fields are read in (#86).** Each refused connect call and the refresh plant what their request carried in the type and the trace id, the refresh's own log line is read, the authorize URL's `response_type` is pinned, and the number reading, the unreadable bodies and the dropped control characters each have their cases.
