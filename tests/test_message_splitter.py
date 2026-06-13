from common.message_splitter import split_text_bubbles


def test_split_msg_then_short_sentence_fallback():
    text = "在想一篇稿子的事……[MSG]有个作者的措辞改了好几轮还是不太对，卡在那儿有点烦。\n\n你呢，今天怎么有空找我？"

    assert split_text_bubbles(text) == [
        "在想一篇稿子的事……",
        "有个作者的措辞改了好几轮还是不太对，卡在那儿有点烦。",
        "你呢，今天怎么有空找我？",
    ]


def test_split_plain_short_chinese_sentences():
    assert split_text_bubbles("嗯。说说看。") == ["嗯。", "说说看。"]


def test_keep_single_clause_without_terminal_punctuation():
    assert split_text_bubbles("一米六三左右吧，怎么突然问这个") == ["一米六三左右吧，怎么突然问这个"]


def test_keep_url_and_markdown_together():
    assert split_text_bubbles("你看这个：https://example.com/a?x=1。别点错。") == [
        "你看这个：https://example.com/a?x=1。别点错。"
    ]
    assert split_text_bubbles("- 第一条。\n- 第二条。") == ["- 第一条。\n- 第二条。"]
