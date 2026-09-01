# Lessons Learned (実測で見つかった落とし穴)

すべて本作業中に実際に発生し、原因分類 → 修正 → 再試行で回復した事例。

## SQLite / State

1. **schema列忘れ** — `runs` tableに `prompt_path` 列を定義し忘れ、初回run時に
   `OperationalError` でrunnerが即死。CREATE TABLEとINSERTの列整合を
   起動時にassertするべきだった。
2. **zombie process** — `os.kill(pid, 0)` はzombieに対して成功する。
   supervisor runnerをkillした後のreconcileが「生存」と誤判定。
   → `/proc/<pid>/stat` のstate fieldを読み `Z` をdead扱いに。

## Process Management

3. **pkill自己一致** — `pkill -f "bridge/bridge.py"` は同じ文字列を含む
   呼び出し元shell自身をkillする。ssh経由の複合コマンドで頻発。
   → `pkill -f "bridge/bridge[.]py"` (bracket trick) か、killと
   pathを含む処理を別callに分離。
4. **nohup + sshのhang** — background起動したprocessがssh sessionの
   stdioを掴むとssh clientが終了しない。stdout/stderrを明示redirectして
   完全detachedにする。

## Hermes / LLM Runtime

5. **`--ignore-rules` は個人context注入を止めない** — CLI helpの記述を信用せず
   `prompt-size` で実測したところ、compositionが一切変化しなかった。
   「安全なはず」のflagはruntimeで検証する。
6. **MCP spawn時のenv sanitization** — MCP SDKのstdio clientは
   最小限のenvしか子processに渡さない。`HERMES_HOME` 等が欠落し、
   provider credential解決が壊れる。module位置からhomeを導出して補正。
7. **CLI引数順** — `-z PROMPT` は値を取るため、flagは `-z` より前に置く。
   順序誤りはexit code 2(usage error)として現れる。

## Container / Bridge

8. **container内path vs host path** — bridgeがcoding agentへprompt fileを
   渡す際、host pathのまま渡すとcontainer内に存在せず、agentは空入力を
   受ける(結果: behavior判定が常にdefault)。mount先pathを渡す。
9. **read-only rootfs + run_user** — `--read-only` + `--user 1000` の
   containerでは、書込可能領域(/workspace, /tmp, /agent-home)以外への
   file作成は全てEROFS。これがworktree escape保護の主防御になる。
10. **makefile mode** — Python 3.13で `makefile("rwd")` はinvalid。
    `"rw"` が正。
11. **prompt-size のflag反映** — subcommand前に置くglobal flagの一部は
    prompt assemblyに反映されない場合がある。セキュリティ判断は
    compositionの実測差分で行う。

## Process Discipline

12. **エラー即報告で停止しない** — 分類(transient / config / bug /
    environmental / security boundary) → 修正 → 別方式 → validation の
    ループで、安全な代替が存在する限りユーザーに問い合わせない。
    destructive / security boundary のみ即STOP。
13. **長時間buildはbackground + poll** — Pi上のdocker build / npm install は
    想定より桁違いに遅い。foreground待ちは全体を固まらせる。
    `nohup … > log 2>&1 &` で起動し、logをpollする。
