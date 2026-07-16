mod cli;
mod tree;

use cli::Command;

fn main() -> anyhow::Result<()> {
    let cli = cli::parse();

    match cli.command {
        Some(Command::Launch { args }) => launch(args),
        Some(Command::Install { source, channel }) => install(source, channel),
        Some(Command::Apply {
            source,
            version,
            notify_file,
            relaunch_app,
            report,
        }) => apply(source, version, notify_file, relaunch_app, report),
        Some(Command::Rollback) => rollback(),
        Some(Command::Status { check, json }) => status(check, json),
        Some(Command::Adopt {
            from_checkout,
            source,
            undo,
        }) => adopt(from_checkout, source, undo),
        Some(Command::SelfRestage) => self_restage(),
        None => {
            // Should not happen — parse() fills in a default.
            unreachable!("cli::parse() should always set a command")
        }
    }
}

fn launch(_args: Vec<String>) -> anyhow::Result<()> {
    todo!("launch: resolve tree → build env → exec venv python (task 1.2)")
}

fn install(_source: Option<String>, _channel: String) -> anyhow::Result<()> {
    todo!("install: download → verify → stage → preflight → flip (task 1.4)")
}

fn apply(
    _source: Option<String>,
    _version: Option<String>,
    _notify_file: Option<String>,
    _relaunch_app: Option<String>,
    _report: String,
) -> anyhow::Result<()> {
    todo!("apply: download → verify → stage → preflight → flip → restage (task 1.4)")
}

fn rollback() -> anyhow::Result<()> {
    todo!("rollback: rewrite current.txt from previous.txt (task 1.4)")
}

fn status(_check: bool, _json: bool) -> anyhow::Result<()> {
    println!("hermes-updater 0.1.0 (stub — task 1.4 implements status)");
    Ok(())
}

fn adopt(
    _from_checkout: Option<String>,
    _source: Option<String>,
    _undo: bool,
) -> anyhow::Result<()> {
    todo!("adopt: migrate legacy checkout to slots (phase 2)")
}

fn self_restage() -> anyhow::Result<()> {
    todo!("self-restage: replace staged binary from current slot (task 1.6)")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_status_works() {
        // status is the one verb that isn't a stub — it prints a version line.
        assert!(status(false, false).is_ok());
    }
}
