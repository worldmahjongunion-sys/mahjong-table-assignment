"""テスト全体の共通設定。

■ AppTestの制限時間
streamlit.testing.v1.AppTestは、画面1回分の実行(at.run())に既定で3秒の制限時間を持ち、
超えると失敗する。この制限時間は「止まってしまったテストを見つける」ためのもので、
速さを確かめるためのものではない。

このテスト群で最初にapp.pyを動かすテスト(test_app_flows.pyの先頭)は、スクリプトの
読み込みや依存ライブラリ(stripe・streamlit_authenticatorなど)の読み込みを最初に
引き受けるため、手元のPCで約1.4秒かかる(2回目以降は約0.2秒)。3秒の既定値では余裕が
半分しかなく、PCの起動直後などファイルの読み込みが遅いときに、何も壊していないのに
たまに赤になっていた(2026-09-24に1回発生。単独で再実行すると緑。原因の断定までは
できていないが、発生したのはセッション最初の全件実行の最初の画面テストだけだった)。

何も壊していないのに赤になるテストがあると「どうせまた例のやつ」と赤を無視する癖が
つき、本当に壊れたときに見逃す。そのため既定値を30秒に広げる。本当に止まったテストは
これまでどおり(30秒で)失敗する。個別のテストがdefault_timeoutを渡した場合はそちらを使う。
"""
import pytest
from streamlit.testing.v1 import AppTest

APP_TEST_DEFAULT_TIMEOUT_SECONDS = 30

_original_from_file = AppTest.from_file.__func__


@pytest.fixture(autouse=True)
def _generous_app_test_timeout(monkeypatch):
    def from_file(cls, script_path, *, default_timeout=APP_TEST_DEFAULT_TIMEOUT_SECONDS):
        return _original_from_file(cls, script_path, default_timeout=default_timeout)

    monkeypatch.setattr(AppTest, "from_file", classmethod(from_file))
