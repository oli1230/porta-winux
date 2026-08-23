# test/ — the three test tiers

| tier | command | speed | what it proves |
|------|---------|-------|----------------|
| unit (`unit/`) | `make unit` | instant | pure logic: manifest parsing, path sandboxing, conflict hashing. No restic needed. |
| smoke (`smoke.sh`) | `make smoke` | seconds | the full real cycle — init → snapshot → checkout/commit → sync → conflict → revert → restore — against a real restic repo, entirely inside a throwaway /tmp sandbox. Never touches your actual files. |
| container (`container/`) | `make container-test` | ~1 min | the same smoke test inside a pristine Fedora 43 container, catching "works on my machine" issues. |
| VM (`vm/`) | `make vm-up && make vm-test` | minutes | a real booted Fedora 43 with SELinux, a simulated external drive, drive auto-detection, and hooks. See vm/README.md. |

Run cheap tiers constantly, expensive tiers before trusting a change.
`smoke.sh` is also the best *reading* introduction to the tool: it is a
complete, commented usage session.
