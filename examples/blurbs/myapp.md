The project is myapp, at https://git.example.com/example/myapp. Clone it to work on it.

Its logs are JSON lines with `ts`, `level`, `msg` and `request_id` fields. A request is traced by grepping for its `request_id`. `level=error` lines from `db.pool` during a deploy are expected and can be ignored.
