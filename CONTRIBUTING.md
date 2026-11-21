# Contributing

Bug reports, ideas and pull requests are all welcome. This is a small project with one maintainer, so replies can
take a few days.

## Reporting a bug

Open an issue with the **Bug report** form. The more of this it has, the faster it gets fixed:

- the app's version (the release you installed) and the plugin's (`hermes 3ds doctor` prints it);
- your Hermes version (`hermes --version`) and what the 3DS is (Old or New, 3DS or 2DS);
- what you did, what you expected, and what happened;
- the output of `hermes 3ds doctor` when the 3DS can't connect or pair.

Security problems go through the **Security** tab instead, never a public issue: see [SECURITY.md](SECURITY.md).

Problems in Hermes itself (the agent, its models, its other platforms) belong to
[Nous Research](https://github.com/NousResearch/hermes-agent).

## Suggesting a feature

Open an issue with the **Feature request** form and say what you would like to do from the 3DS. Please open an
issue before writing a large change, so the work isn't wasted if it doesn't fit.

## Pull requests

1. Fork the repository and make a branch from `main`.
2. Build and test (below). Both test suites should pass, and the app should build without warnings.
3. Keep a pull request to one change, and say what you tested and on what (emulator, Old 3DS, New 3DS).
4. If the change is something a user would notice, add a line to `CHANGELOG.md` under "Unreleased".

`main` here gets one commit per release, made from a development tree. An accepted pull request is carried into
that tree and comes out in the next release, with your name in the commit and the release notes, so the pull
request itself is closed rather than merged.

### Building and testing

The README's [Building from source](README.md#building-from-source) section lists what to install.

| Part | Build | Test |
|---|---|---|
| App (`app/`, C with libctru and citro2d) | `app/build.sh` | `app/test/run_tests.sh` |
| Plugin (`plugin/`, Python) | nothing to build | `pip install -r plugin/requirements-dev.txt`, then `python -m pytest` in `plugin/` |

No console is needed for most work:

- `tools/fake3ds.py` is a 3DS client for the terminal, for trying the plugin.
- `tools/mock_pc.py` is a scripted server, for trying the app in an emulator.
- `tools/hermes_compat.py` checks the plugin against a newer Hermes without starting a gateway.

A change to what the app and plugin say to each other also needs [docs/protocol.md](docs/protocol.md) updated, and
has to keep an older app working with a newer plugin and the other way round.

### Style

- **App:** C, four-space indents, no compiler warnings. The app has to run on the Old 3DS, so mind memory and
  speed.
- **Plugin:** Python. Everything that reads Hermes' own state lives in `plugin/compat.py`, and only
  Hermes' public interfaces are used (a rule of the Hermes plugin catalog).
- **Words on screen:** short and plain.

## Licence

The project is under the [MIT licence](LICENSE). By sending a contribution you agree it is published under the
same licence.
