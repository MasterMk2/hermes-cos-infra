# Offline test environment

Gateway / Attention / Planning / Calendar Tentativeを同じコマンドで検査するテスト専用環境。
Calendar Tentativeは実スキーマのDBと偽のGoogleを使い、Googleへは接続しない。
既存サービスのvirtualenv、install-root.sh、systemd、資格情報には触らない。

## Runtime と凍結復元

CIはUbuntu 24.04、CPython 3.12 / 3.13。3.12は今回のクラウド試験環境、
3.13はリポジトリ内のPi用依存説明と整合する範囲を検査する。x86_64 CIはPi実機/arm64検査の代わりにはならない。
Pythonは公式actions/setup-pythonから取得し、action本体はcommit SHAに固定している。

```sh
python --version
python -m venv <TEST_VENV>
<TEST_VENV>/bin/python -m pip install --require-hashes -r requirements-test.txt
<TEST_VENV>/bin/python -m pip check
<TEST_VENV>/bin/python scripts/check_offline.py
```

doctorは最初のversionとpip check、syncはhash必須の復元、checkは最後のコマンド。
環境変数一覧や秘密情報を出力しない。WindowsのvenvはScripts/python.exeを使うが、
Unix socket試験を含む正式CIの対象OSはLinux。新しいtask runnerやプロジェクト管理方式は導入しない。

`requirements-test.in`が意図、`requirements-test.txt`が推移依存・version・hashを含む生成物。
通常のcheckは依存を変更しない。lock更新だけを行う場合は生成物冒頭のuv pip compileコマンドを使い、
差分をレビューする（生成時uv 0.12.19）。pipによる復元にはuvを必要としない。
lockはPython 3.12以降向けに生成し、3.13で必要なaudioop-ltsもmarker付きで保持する。

## チェックの境界

入口はsuiteを別プロセスで走らせ、同名test moduleの衝突を避ける。
pytestの外部自動pluginと継承した追加引数を無効にし、既存ログ試験とpytestログhandlerの干渉を避ける。
`offline_guard`はPythonテストプロセスのIP接続・DNSを拒否する。例外を被験コードが捕捉しても、
試行があればsuite全体を失敗にする。ローカルAF_UNIXと合成SQLiteは許可し、実transportはfake。
これは本番のネットワーク/権限設定を変えるものではない。

依存ダウンロードとGitHub checkoutにはネットワークが必要。check中に外部サービスへは接続しない。
プライベートdenylist、実資格情報、実DBをCIへ入れない。
既存のport 1疎通試験はURLopenの拒否fixtureへ、disk閾値試験は合成容量へ変更し、環境依存を取り除く。

Planningの「I.2で既存moduleを変更しない」という履歴検査は、当該Phaseの終端
`640d9db1044a68612e2b9fdf0204a68ebef9ea0f`までを検査する。
今回の後続修正をprivacy例外に偽装せず、歴史上の検査範囲を固定する。
現HEAD全体のprivacy scanは狭めない。CIのcheckoutは全履歴を取得する。

## 結果の読み方

- exit 0: 選択した全suiteが成功。各テストのskip理由は別途残る
- exit 1: いずれかのsuite失敗、またはIP/DNS試行を検出
- exit 2: `--without-unix-sockets`を明示した部分実行。成功扱いにしない

今回のクラウド環境ではAF_UNIX socket作成がEPERMとなったため、18件の既存socket試験を
手元では実行できなかった。必要なら同じ入口に`--without-unix-sockets`を指定して残りを検査できるが、
CIにはこの引数を付けない。実handler+偽transport+DB再接続の追加試験はその制約下でも実行できる。
完全なGit履歴/全tree、非公開denylist、現在のPi/実配送は異なる検証であり、数を合算して合格としない。
