# Shared inspector v1 inventory identity

This fixture-free library contains only the unchanged ten-field storage-role data
type and Python-compatible canonical JSON/SHA-256 inventory generation, extracted
from the offline fixture prototype. It contains no observations, fixture backend,
listener, filesystem authority, native quota query or service entrypoint.

Both the [fixture package](../quota-inspector/README.md) and
[native foundation](../quota-inspector-native/README.md) depend on it; the native
package never depends on the fixture package. Existing shared golden tests
continue to verify DEL, controls, Unicode, key/role ordering and digest stability.
These helpers encode identity, not inventory trust or execution authorization.
