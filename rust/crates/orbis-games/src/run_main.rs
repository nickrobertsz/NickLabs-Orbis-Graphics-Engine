fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().map(|a| a == "--help" || a == "-h").unwrap_or(false) || argv.is_empty() {
        eprintln!("orbis-run [--profile FILE | --name NAME] [--title T] [--appid N] [--runner wine|proton:<id>] [--dry-run] [--no-scope] [--] <command...>\n\
                   Steam launch options: orbis-run %command%");
        std::process::exit(if argv.is_empty() { 2 } else { 0 });
    }
    std::process::exit(orbis_games::run::main_run(argv));
}
