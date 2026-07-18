# Third-party source

`social-media-copilot/` vendors the GPL-3.0 `server` branch of
`iszhouhua/social-media-copilot` at commit
`b9d2923b5f2f1ea6146f99f278f943d3079b3fa0`.

It is used as the local Xiaohongshu detail adapter. The local patch removes
request/response logging and binds its bridge service to `127.0.0.1` only.

