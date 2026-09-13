"""Two real trace families, taken from a client fleet's production logs.

They are here because grouping is the one rule in this library that cannot be
tested on invented data. A synthetic trace differs from its sibling exactly
where the author decided it should, which makes any threshold look right.
These differ where production actually made them differ — three uuids in one
family, a user id and a trace id in the other — and one of them differs in a
way that must NOT group (a different constraint, i.e. a different bug in the
same function).

Family 1 — a foreign key violation. A bus consumer created a row for a user
whose shadow row did not exist yet; 22 occurrences over 3 events, every one
retried and then parked. The uuid in ``DETAIL`` is the only text that varies
between occurrences, and there were three of them.

Family 2 — a JWT refusal. An in-flight request arrived with the cookie of a
guest account that had just been merged into a real one and deleted. One
occurrence per stale cookie; the user id and the trace id vary.

The service/package names are the framework's own (``stapel_core``,
``stapel_recordings``); the client's own module names are not reproduced.
"""

FK_VIOLATION_A = '''Traceback (most recent call last):
  File "/opt/venv/lib/python3.12/site-packages/django/db/backends/utils.py", line 105, in _execute
    return self.cursor.execute(sql, params)
psycopg2.errors.ForeignKeyViolation: insert or update on table "recordings_zoom_ingest_settings" violates foreign key constraint "recordings_zoom_ingest_settings_user_id_943d5923_fk_users_id"
DETAIL:  Key (user_id)=(13484e5c-6652-4547-b0ac-a196393b4708) is not present in table "users".

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/opt/venv/lib/python3.12/site-packages/stapel_core/comm/actions.py", line 248, in deliver_to_subscribers
    handler(event)
  File "/app/recordings_service/actions.py", line 176, in on_personal_workspace_created
    ZoomIngestSettings.objects.get_or_create(user_id=user_id)
  File "/opt/venv/lib/python3.12/site-packages/django/db/models/query.py", line 949, in get_or_create
    return self.create(**params), True
  File "/opt/venv/lib/python3.12/site-packages/django/db/backends/utils.py", line 105, in _execute
    return self.cursor.execute(sql, params)
django.db.utils.IntegrityError: insert or update on table "recordings_zoom_ingest_settings" violates foreign key constraint "recordings_zoom_ingest_settings_user_id_943d5923_fk_users_id"
DETAIL:  Key (user_id)=(13484e5c-6652-4547-b0ac-a196393b4708) is not present in table "users".
'''

#: The same bug, a different account. This is 18-of-22 vs 2-of-22 in the log;
#: nothing but the uuid differs.
FK_VIOLATION_B = FK_VIOLATION_A.replace(
    "13484e5c-6652-4547-b0ac-a196393b4708",
    "7d8224fd-1c2e-4a55-9f0a-6b2e4c8d1e33",
)

#: And the third account.
FK_VIOLATION_C = FK_VIOLATION_A.replace(
    "13484e5c-6652-4547-b0ac-a196393b4708",
    "8de1e24b-77b2-4e51-8c4d-2a9f6b3c7e10",
)

#: A DIFFERENT constraint on the same table, raised from the same function.
#: Same frames, same exception class, same length — and a different bug, which
#: is fixed by a different change. Grouping this with the family above would
#: close a live defect by fixing another one.
FK_VIOLATION_OTHER_CONSTRAINT = FK_VIOLATION_A.replace(
    "recordings_zoom_ingest_settings_user_id_943d5923_fk_users_id",
    "recordings_zoom_ingest_settings_workspace_id_a1b2c3d4_fk_workspaces_id",
).replace("Key (user_id)=", "Key (workspace_id)=")

JWT_REFUSAL_A = '''Traceback (most recent call last):
  File "/opt/venv/lib/python3.12/site-packages/stapel_core/django/jwt/authentication.py", line 141, in authenticate
    raise AuthenticationFailed(self._refusal(user_data, reasons))
  File "/opt/venv/lib/python3.12/site-packages/rest_framework/views.py", line 480, in dispatch
    self.initial(request, *args, **kwargs)
  File "/opt/venv/lib/python3.12/site-packages/rest_framework/views.py", line 398, in initial
    self.perform_authentication(request)
rest_framework.exceptions.AuthenticationFailed: JWT authentication refused: user 285ea0cd-5108-4bb3-8b9e-5a1fcd013961 was deleted at the issuer [trace=e3a120ed-4a2c-4a1e-9d51-1f2c3b4a5d6e] path=/billing/api/v1/subscription
'''

#: A second stale cookie, minutes later: a different account, a different
#: trace id, the same refusal.
JWT_REFUSAL_B = (
    JWT_REFUSAL_A
    .replace("285ea0cd-5108-4bb3-8b9e-5a1fcd013961", "1b7218dc-9e0f-4c33-b7a2-8d4e5f6a7b8c")
    .replace("e3a120ed-4a2c-4a1e-9d51-1f2c3b4a5d6e", "605a6b2b-2f14-4e8c-9a3d-7c1b2e3f4a5b")
)

#: Four frames or fewer — the "short trace" case the grouping rule treats
#: differently. Two of these that are not identical after normalisation must
#: NOT be merged on a similarity score.
SHORT_A = '''Traceback (most recent call last):
  File "/app/svc/tasks.py", line 12, in run
    provider.transcribe(job)
RuntimeError: STT provider quota exhausted
'''

SHORT_B = '''Traceback (most recent call last):
  File "/app/svc/tasks.py", line 40, in run
    provider.summarise(job)
RuntimeError: STT provider quota exhausted
'''

__all__ = [
    "FK_VIOLATION_A",
    "FK_VIOLATION_B",
    "FK_VIOLATION_C",
    "FK_VIOLATION_OTHER_CONSTRAINT",
    "JWT_REFUSAL_A",
    "JWT_REFUSAL_B",
    "SHORT_A",
    "SHORT_B",
]
