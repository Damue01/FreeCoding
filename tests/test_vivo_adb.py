from app2api.drivers.vivo_adb import (
    VivoAdbDriver,
    extract_answer_for_copy,
    extract_completed_answer,
    extract_xiaohuoren_control_answer,
    find_copy_action,
    find_send_action,
    find_small_tuan_entry,
    find_xiaohuoren_card_action,
    is_functional_small_tuan_page,
    parse_bounds,
    parse_ui_xml,
)
from app2api.ocr import OcrLine


def test_parse_bounds():
    assert parse_bounds("[60,1069][1380,1812]") == (60, 1069, 1380, 1812)
    assert parse_bounds("bad") is None


def test_find_small_tuan_entry_prefers_bottom_navigation():
    nodes = [
        {"text": "小团", "bounds": "[10,100][100,200]"},
        {"content-desc": "小团", "bounds": "[360,3020][720,3200]"},
        {"text": "首页", "bounds": "[0,3020][360,3200]"},
    ]
    assert find_small_tuan_entry(nodes) == nodes[1]


def test_find_send_action_never_selects_voice_capsule():
    nodes = [
        {
            "resource-id": "com.sankuai.meituan:id/iv_capsule_audio_btn",
            "class": "android.widget.ImageView",
            "clickable": "true",
            "bounds": "[1200,2800][1400,3100]",
        },
        {
            "resource-id": "com.sankuai.meituan:id/iv_expanded_input_btn",
            "class": "android.widget.ImageView",
            "clickable": "true",
            "bounds": "[1200,1800][1400,2000]",
        },
    ]
    assert find_send_action(nodes) == nodes[1]


def test_functional_page_requires_session_marker_not_only_input():
    input_node = {
        "resource-id": "com.sankuai.meituan:id/ai_search_input_bar",
        "bounds": "[0,2704][1440,3072]",
    }
    assert is_functional_small_tuan_page([input_node]) is False
    assert (
        is_functional_small_tuan_page(
            [
                input_node,
                {
                    "resource-id": "com.sankuai.meituan:id/dka",
                    "text": "小团",
                },
            ]
        )
        is True
    )


def test_extract_completed_answer_chooses_narrative():
    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy rotation="0">
      <node text="问题" resource-id="com.sankuai.meituan:id/tv_ai_small_tuan_query_text" bounds="[1,1][2,2]" />
      <node text="已完成思考" resource-id="com.sankuai.meituan:id/small_tuan_thinking_v2_tv_title" bounds="[1,2][2,3]" />
      <node text="地点标题" resource-id="" bounds="[1,3][2,4]" />
      <node text="这是一段更长的完整回答，应该被稳定提取。" resource-id="" bounds="[10,20][300,400]" />
      <node text="猜你想问" resource-id="com.sankuai.meituan:id/small_tuan_sug_tv" bounds="[1,5][2,6]" />
      <node text="不应进入回答的建议问题" resource-id="com.sankuai.meituan:id/aixiaotuan_text_tv" bounds="[1,6][2,7]" />
    </hierarchy>"""
    result = extract_completed_answer(parse_ui_xml(xml))
    assert result == (
        "这是一段更长的完整回答，应该被稳定提取。",
        (10, 20, 300, 400),
    )


def test_extract_answer_waits_until_completed():
    xml = """<hierarchy><node text="正在搜索" resource-id="com.sankuai.meituan:id/small_tuan_thinking_v2_tv_title" bounds="[1,1][2,2]" /></hierarchy>"""
    assert extract_completed_answer(parse_ui_xml(xml)) is None


def test_extract_completed_answer_is_bound_to_latest_question():
    nodes = [
        {
            "resource-id": "com.sankuai.meituan:id/tv_ai_small_tuan_query_text",
            "text": "第一问",
        },
        {
            "resource-id": "com.sankuai.meituan:id/small_tuan_thinking_v2_tv_title",
            "text": "已完成思考",
        },
        {"resource-id": "", "text": "第一轮回答", "bounds": "[1,1][2,2]"},
        {"resource-id": "com.sankuai.meituan:id/small_tuan_sug_tv", "text": "猜你想问"},
        {
            "resource-id": "com.sankuai.meituan:id/tv_ai_small_tuan_query_text",
            "text": "第二问",
        },
        {
            "resource-id": "com.sankuai.meituan:id/small_tuan_thinking_v2_tv_title",
            "text": "已完成思考",
        },
        {"resource-id": "", "text": "第二轮回答", "bounds": "[3,3][4,4]"},
        {"resource-id": "com.sankuai.meituan:id/small_tuan_sug_tv", "text": "猜你想问"},
    ]
    assert extract_completed_answer(nodes, "第二问") == (
        "第二轮回答",
        (3, 3, 4, 4),
    )


def test_find_copy_action_is_bound_to_latest_question():
    nodes = [
        {
            "resource-id": "com.sankuai.meituan:id/tv_ai_small_tuan_query_text",
            "text": "第一问",
        },
        {
            "resource-id": "com.sankuai.meituan:id/aixiaotuan_feedback_copy_button",
            "clickable": "true",
        },
        {
            "resource-id": "com.sankuai.meituan:id/tv_ai_small_tuan_query_text",
            "text": "第二问",
        },
        {
            "resource-id": "com.sankuai.meituan:id/aixiaotuan_feedback_copy_button",
            "clickable": "true",
        },
    ]
    assert find_copy_action(nodes, "第二问") == nodes[3]


def test_extract_completed_answer_can_preserve_markdown_layout():
    nodes = [
        {
            "resource-id": "com.sankuai.meituan:id/tv_ai_small_tuan_query_text",
            "text": "问题",
        },
        {
            "resource-id": "com.sankuai.meituan:id/small_tuan_thinking_v2_tv_title",
            "text": "已完成思考",
        },
        {
            "resource-id": "",
            "text": "## 标题\n\n- 第一项\n- 第二项",
            "bounds": "[1,2][3,4]",
        },
        {"resource-id": "com.sankuai.meituan:id/small_tuan_sug_tv", "text": "猜你想问"},
    ]
    assert extract_completed_answer(nodes, "问题", preserve_formatting=True) == (
        "## 标题\n\n- 第一项\n- 第二项",
        (1, 2, 3, 4),
    )


def test_copy_anchor_extracts_long_answer_when_question_is_offscreen():
    nodes = [
        {
            "resource-id": "com.sankuai.meituan:id/tv_ai_small_tuan_query_text",
            "text": "控件树里残留的旧问题",
            "bounds": "[60,0][1380,100]",
        },
        {
            "resource-id": "",
            "class": "android.widget.TextView",
            "text": "第一段\n\n- 项目一\n- 项目二",
            "bounds": "[60,0][1380,2000]",
        },
        {
            "resource-id": "com.sankuai.meituan:id/aixiaotuan_feedback_copy_button",
            "clickable": "true",
            "bounds": "[60,2060][165,2165]",
        },
    ]
    copy_action = find_copy_action(nodes, "已经滚出屏幕的问题")
    assert copy_action == nodes[2]
    assert extract_answer_for_copy(nodes, copy_action) == (
        "第一段\n\n- 项目一\n- 项目二",
        (60, 0, 1380, 2000),
    )


def test_merge_ocr_rows_repairs_overlapping_lingbao_fragments():
    lines = [
        OcrLine(
            "我是王者峡谷里迷人又可爱的小知己，是你白",
            0.97,
            (375, 1024, 1242, 1058),
        ),
        OcrLine(
            "爱的小知己，是你的好帮手，你有什么不懂的都可",
            0.99,
            (860, 1022, 1840, 1057),
        ),
        OcrLine(
            "你有什么不懂的都可以问我，比如问我赵云怎么玩",
            0.98,
            (1460, 1024, 2438, 1058),
        ),
        OcrLine(
            "北如问我赵云怎么玩？",
            0.91,
            (2058, 1023, 2462, 1058),
        ),
    ]

    merged = VivoAdbDriver._merge_ocr_rows(lines, 45)

    assert [line.text for line in merged] == [
        "我是王者峡谷里迷人又可爱的小知己，是你的好帮手，"
        "你有什么不懂的都可以问我，比如问我赵云怎么玩？"
    ]


def test_find_xiaohuoren_card_after_current_question():
    nodes = [
        {"content-desc": "小火人 旧问题", "resource-id": "x:id/kd8"},
        {"resource-id": "x:id/gen", "clickable": "true"},
        {"text": "小火人 新问题", "resource-id": "x:id/kd8"},
        {"resource-id": "x:id/gen", "clickable": "true"},
    ]

    assert find_xiaohuoren_card_action(nodes, "小火人 新问题") == nodes[3]


def test_extract_xiaohuoren_control_answer_joins_left_bubbles_only():
    nodes = [
        {
            "content-desc": "小火人 今天开心吗",
            "resource-id": "x:id/kd8",
            "bounds": "[600,100][1300,200]",
        },
        {
            "content-desc": "当然开心",
            "resource-id": "x:id/kd8",
            "bounds": "[210,300][900,400]",
        },
        {
            "content-desc": "因为你来找我了",
            "resource-id": "x:id/kd8",
            "bounds": "[210,420][1000,520]",
        },
    ]

    assert extract_xiaohuoren_control_answer(nodes, "小火人 今天开心吗") == (
        "当然开心\n因为你来找我了",
        (210, 300, 1000, 520),
    )
