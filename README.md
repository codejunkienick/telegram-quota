# telegram-quota

A `/quota` command for the Telegram bridge pack. It reads how much of each AI
subscription's usage windows is spent, through
[aiquokka](https://github.com/McKean/aiquokka) and through reading files
other programs write, and answers with one monospace table: the colour dot,
the window, the share used, when it resets and the time left until then.

```text
Claude · personal
🟢 5h        5%  Sat 19:59  4h00m
🟢 week     56%  Wed 10:59  3d19h
🟢 Fable wk  9%  Wed 10:59  3d19h
Codex · work
⚪ 5h         –  no window
🟢 week      7%  Thu 11:24  4d19h
```

It is one Python file (`quota_command.py`, Python 3.11 or later, standard
library only) that speaks the bridge's command-handler contract: one JSON
object on stdin, one `{"actions": [...]}` object on stdout, exit status 0
always. It runs no shell, gives every child an argument list, closed stdin, a
capped output, a deadline and its own process group, and writes nothing but
its optional cache.

## Install

1. Clone this repository anywhere the bridge's user can read, for example
   `/path/to/telegram-quota`.
2. Write the command's config as `/path/to/quota.toml`. See Config below.
   `quota.example.toml` has every key. The config is owned by the bridge's
   user, chmod 600.
3. Write a wrapper. The bridge runs a handler with no arguments. Save it as
   `/path/to/bin/telegram-quota`, owned by the bridge's user, chmod 700, in a
   directory that neither group nor others can write. The bridge refuses the
   settings otherwise.

   ```sh
   #!/bin/sh
   # The /quota command of the Telegram bridge pack.
   exec /usr/bin/python3 -I /path/to/telegram-quota/quota_command.py --config /path/to/quota.toml
   ```

4. Add the command to the bridge's settings:

   ```toml
   [[command]]
   name = "quota"
   description = "Show subscription quota"
   handler = "/path/to/bin/telegram-quota"
   timeout = 20
   ```

   With `cache_dir` set, also list that directory in the settings'
   `handler_write_paths` (the handler runs in the bridge's sandbox, which
   writes nowhere else); without a cache leave it out. The bridge passes its
   `timezone` setting as `TZ`, which the table's times use (UTC when it is
   unset or unknown).

5. Run the bridge's install step again, restart it and refresh its command
   menu, as its documentation says for any added command. A settings file
   that still has a [quota] table does not load: delete that table.

Remove the command by deleting the table (and the write path) and doing the
same steps.

## Config

A TOML file, read strictly: an unknown key, a wrong type or a path that is not
absolute makes the whole reply the one row `⚪ quota unavailable: <reason>`,
naming the key (never a path).

| key | default | meaning |
| --- | --- | --- |
| `aiquokka` | none | absolute path of the aiquokka executable; required when `providers` is not empty |
| `providers` | `["claude", "codex", "muse", "antigravity", "grok", "deepseek"]` | the aiquokka providers to read, in table order; each 1-32 of `a-z`, `0-9`, `-`; `[]` reads only the reading files |
| `provider_timeout_seconds` | `8` | seconds each provider may take, 1-15 |
| `deepseek_key_file` | none | absolute path of a file owned by the user running the handler, chmod 600, holding the DeepSeek API key; its text is given to the deepseek read only, as `DEEPSEEK_API_KEY`; a missing or unsafe file means no key |
| `path_prepend` | `[]` | absolute directories put before `/usr/local/bin:/usr/bin:/bin` in `PATH` for aiquokka and the CLIs it runs (`muse`, `agy`, ...) |
| `readings` | `[]` | absolute paths of reading files (below), in table order |
| `cache_dir` | none | absolute path of an existing directory for the cache (below) |
| `[labels]` | none | provider name to a label of 1-32 characters shown in its header, as in `Claude · personal` |

The bridge gives a handler a fixed `PATH`. A CLI that aiquokka runs and
that lives elsewhere needs its directory in `path_prepend`.

Every provider is read in parallel by `aiquokka <provider> --json
--no-refresh`, with the handler's own environment, `PATH` as above, and the
DeepSeek key for deepseek alone. Only each window's `label`, `used_percent`
and `resets_at` are used: the account, its email and the plan are never shown.
The reading files are read while the providers are. The whole run ends within
15 seconds; a provider still running then, or a reading file not read by then,
shows `⚪ unavailable: timeout`.

## The table

The answer is a plain message, not a reply to the `/quota` message.

Per source one header, `<Provider>` or `<Provider> · <label>`, then one row per
window:

- the dot: 🟢 under 70 %, 🟡 70-89 %, 🔴 90 % and over, ⚪ no number;
- the label, shortened: a trailing ` Limit` or ` Remaining` is dropped, `5h`
  stays, `Weekly` is `week`, `Weekly Fable` is `Fable wk`, then 8 ASCII
  characters at most; a `<group> - <span>` label is a short group and a short
  span (`Gemini Models - Weekly` is `Gem wk`, `Claude and GPT models - 5h` is
  `C+GPT 5h`, `Gemini 4 Argon - Weekly` is `Argon wk`, another group its
  first word cut to fit); when two rows of one source would still read the
  same, that source shows its whole labels (32 characters at most) in a
  wider column;
- the share used, rounded to a whole percent;
- the reset, as `Ddd HH:MM` in `TZ`, and the time left: `45m` under an hour,
  `4h00m` under a day, else `3d19h`; `–` once the reset has passed; a window
  with no reset is `–  no window`.

A source that could not be read shows one row `⚪ unavailable: <reason>`; a
reason never carries an email (`<account>`) or a path (`<path>`) and is cut to
60 characters. Sources are ordered by provider, in the order of `providers`
and then of the providers only the readings name; within a provider the
aiquokka read comes first, then the readings in file order. A table longer
than a Telegram message (4096 UTF-16 units) loses whole trailing sources and
ends with a line `…`.

## Reading files

A reading file lets another program put windows into the table that aiquokka
cannot read from here, such as a second account of the same provider. It is a
regular file, not a symlink, of at most 64 KiB. It holds exactly this JSON
shape:

```json
{
  "observed_at": "2026-10-17T10:50:00Z",
  "groups": [
    {
      "provider": "Codex",
      "account": "work",
      "windows": [
        {"label": "5h", "used_percent": null, "resets_at": null},
        {"label": "Weekly", "used_percent": 7, "resets_at": "2026-10-22T06:24:00Z"}
      ],
      "error": null
    }
  ]
}
```

- `observed_at`: when the windows were read, ISO-8601 with a zone. A reading
  older than 30 minutes, or more than 5 minutes ahead of the clock, shows each
  of its groups as `⚪ unavailable: stale reading`.
- `groups`: at most 32. `provider` (required) is the header and decides the
  order; `account` (optional) is a free label shown after it; `windows`
  (optional, at most 16 used) as above. Each window needs a `label`.
  `used_percent` is a number or `null`. `resets_at` is ISO-8601 or `null`.
  `error` (optional), when not `null`, shows the group as one row
  `⚪ unavailable: <error>`.
- Any other key, a wrong type, a duplicate key or text that is not strict JSON
  makes the file malformed; a missing, unreadable or malformed file is one
  source named by the file's name with `⚪ unavailable: <reason>`.

The command knows no account: what `account` says is up to the writer.

## The cache

With `cache_dir`, the last good aiquokka answer per provider (windows only) is
kept in one file there, `quota-cache.json`, written whole to a new temporary
file owned by the user running the handler, chmod 600, and renamed into
place. When a provider's live read fails, its
cached answer is shown with the header ` (as of Ddd HH:MM)`; one older than 24
hours is not shown. Some readers only answer while their own app runs; the
cache keeps their last numbers in the table.

## Tests

```sh
scripts/test.sh
```

runs `python3 -B -m unittest discover -s .` from the tree root. The tests use
a fake aiquokka and temporary files only.

## License

MIT, see `LICENSE`.
