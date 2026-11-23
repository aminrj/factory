# Board

_Generated 2026-11-23 19:22 UTC by `scripts/collect_metrics.py`._

## Waiting for you

| Where | Checkpoint | Since | Hours |
|---|---|---|---|
| brevkoll#36 | after_gate3-interrupted: the foreman stopped while running a | 2026-11-23T19:14+00:00 | 0.1 |
| brevkoll#7 | after_gate3-retry: a step failed | 2026-11-22T19:10+00:00 | 24.2 |
| brevkoll#6 | 4: try it, review and merge | 2026-11-22T22:01+00:00 | 21.3 |

## idea

| Item | Issue |
|---|---|
| Support a model endpoint that needs a client certificate (mutual TLS) | brevkoll#14 |

## spec-ready

| Item | Issue |
|---|---|
| (none) | |

## building

| Item | Issue |
|---|---|
| The application returns "The model returned an unusable answer." even after sending a correct pdf file with a Swedish letter in it | brevkoll#36 |
| Show the app version on /healthz and in the page footer | brevkoll#32 |
| Uploaded a 256ko pdf file to the app and got an error File too large. | brevkoll#29 |
| Quality guard green again: split create_app, trace T6 and T15 | brevkoll#26 |
| Ready for the cluster: the model over mutual TLS, and a hardened deploy/ base | brevkoll#19 |
| UI refresh: a professional, fast interface for reading a letter on a phone | brevkoll#15 |
| Chunked uploads without Content-Length are only rejected after parsing | brevkoll#7 |
| OCR misreads characters in the letter, for example the @ in an email address | brevkoll#6 |

## shipped

| Item | Issue |
|---|---|
| brevkoll v1: letter decoder | brevkoll#1 |
| [idea] brevkoll: Swedish letter decoder for parents | factory#1 |

## killed

| Item | Issue |
|---|---|
| (none) | |
