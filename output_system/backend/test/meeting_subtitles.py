"""測定と動作確認に使う、英語の会議の字幕。

文章生成モデルの作成時間とメモリを測るための入力で、実際の会議の記録ではない。
プロジェクトの定例会議を想定して、話題ごとに数行から十数行の発言を書いた。決まったこと（決定事項）と、
担当者が名前で出る宿題を含む。

- sample_meeting(): 約 100 行（107 行）の短い会議（要約と質問の質を、目で確かめる用）
- long_meeting(lines): 話題を繰り返して、指定の行数（2 時間の会議なら約 1,500 行）にした会議。
  繰り返しのたびに、文中の数字をずらして、同じ文が並ばないようにしている。内容は繰り返しのため、
  要約の質の判断には使わない。作成時間とメモリを測るための入力
"""

import re

_TOPICS: list[list[str]] = [
    # 開始と議題
    [
        "Okay, it looks like everyone has joined, so let's get started.",
        "Thanks for making time today, I know the week has been busy.",
        "We have five items on the agenda: the release status, the cloud budget, hiring,",
        "the customer feedback from last month, and the vendor contract renewal.",
        "Mark is going to take notes, and we should finish by the top of the hour.",
        "Can everyone hear me clearly? I'm seeing a thumbs up from Priya, good.",
        "Let's start with the release status since it affects most of the other topics.",
    ],
    # リリースの状況
    [
        "The mobile app release is currently scheduled for November 12th.",
        "Engineering says the build is stable, but we still have seven open bugs.",
        "Three of those are marked as blockers, mostly around the login flow on older phones.",
        "Dana, can you walk us through what the login problem actually is?",
        "Sure. When a user has two accounts linked, the token refresh sometimes fails silently.",
        "We reproduced it on Android 11 and 12, but not on the newer versions.",
        "The fix is small, but it touches the authentication library, so we need a full regression run.",
        "A full regression run takes about two days, so that is the real constraint here.",
        "If we start the regression on Monday, we can still hit the November 12th date.",
        "I don't want to promise that yet, because QA is also covering the payments change.",
        "Let's keep the date for now, and Dana will confirm by Friday whether the fix holds.",
        "We also agreed that no new features go into this release, only bug fixes.",
    ],
    # 予算
    [
        "Moving on to the budget. Our cloud spending was eighteen percent over forecast last quarter.",
        "Most of the overage came from the test environments that were left running at night.",
        "Luis found about forty virtual machines that nobody had touched in two months.",
        "We shut those down last week, which should save roughly six thousand dollars a month.",
        "The second driver is the data transfer cost between the two regions.",
        "We could move the analytics jobs into the same region as the database.",
        "That would cut transfer costs, but it needs a short maintenance window on a weekend.",
        "Finance asked us for a revised forecast by the end of the month.",
        "I think we should plan for a ten percent reduction, not the full eighteen, to stay safe.",
        "Okay, so the decision is to adopt the ten percent target and review it again in January.",
        "Luis will prepare the revised forecast and send it to finance by the thirtieth.",
    ],
    # 採用
    [
        "On hiring, we have two open positions: a backend engineer and a designer.",
        "We interviewed six candidates for the backend role, and two made it to the final round.",
        "The final interviews are next Tuesday and Wednesday, and Priya will lead both of them.",
        "For the designer role, the pipeline is thinner. We only have three applicants so far.",
        "Maybe we should widen the search and allow remote candidates in other time zones.",
        "That raises a question about overlap hours with the team.",
        "A four hour overlap would be enough for daily stand-ups and reviews, in my opinion.",
        "Alright, let's allow remote candidates as long as they overlap at least four hours.",
        "Priya, please update the job posting this week and share it with the recruiters.",
        "I'll also ask the team whether anyone can refer a designer from their network.",
    ],
    # 顧客の声
    [
        "Next, customer feedback. Support received about two hundred tickets last month.",
        "The most common complaint is that exporting reports takes too long.",
        "One customer said a monthly export took almost eleven minutes and then timed out.",
        "That is unacceptable, and it is the kind of thing that makes people cancel.",
        "The second most common request is a dark mode for the dashboard.",
        "Honestly, I think the export problem matters far more than dark mode.",
        "I agree. Let's put the export performance fix at the top of the next sprint.",
        "Sam, can you estimate how long it would take to rewrite the export as a background job?",
        "My first guess is two weeks, including the notification when the file is ready.",
        "Good. Sam will write up the estimate and a short design note by next Wednesday.",
        "We will tell the customers who complained that a fix is planned for December.",
    ],
    # 障害の振り返り
    [
        "Let's talk about the outage on the fourth. For about forty minutes, the API was unavailable.",
        "The root cause was an expired certificate on the internal gateway.",
        "Nobody was alerted because the monitoring only checked the public endpoint.",
        "So the failure was in the alerting, not only in the certificate itself.",
        "We have already renewed the certificate and added an alert thirty days before expiry.",
        "I would also like a list of every certificate we own, with the owner and the expiry date.",
        "That sounds like a good use of an afternoon, and it prevents a repeat of this.",
        "Mei will build the certificate inventory and share it with the whole team by the twentieth.",
        "We should also write a short incident report and send it to the affected customers.",
        "Right, Dana and Mei will draft the report together, and I will review it before it goes out.",
    ],
    # マーケティング
    [
        "Now the marketing plan for the launch. The campaign starts two weeks before the release.",
        "We are planning three emails, a blog post, and a short video demo.",
        "The video is the expensive part. The agency quoted us nine thousand dollars.",
        "Could we record the demo ourselves instead and use the agency only for editing?",
        "That would cut the cost by about half, and the product team can do the recording.",
        "I like that. It also keeps the demo closer to the real product.",
        "The only risk is that the schedule is tight if the release date slips.",
        "Then let's finalize the script this week so recording can happen as soon as the build is ready.",
        "Okay, Rachel will own the script and the email drafts, and Sam will help with the recording.",
    ],
    # ベンダー契約
    [
        "Last item on the agenda is the vendor contract renewal for the monitoring service.",
        "The vendor wants a three year commitment with a seven percent increase each year.",
        "We have been happy with the service, but the price increase is higher than inflation.",
        "Another vendor offered a similar product at roughly twenty percent less.",
        "Switching would take about a month of work, and we would lose some historical data.",
        "I would rather negotiate than switch, because the migration risk is real.",
        "We could ask for a one year renewal with a price freeze and keep the other quote as leverage.",
        "That sounds reasonable. Luis, can you contact the vendor and propose that?",
        "Yes, I'll call them tomorrow and report back at the next meeting.",
        "If they refuse, we will meet again and decide whether to start the migration planning.",
    ],
    # データ移行
    [
        "A quick update on the data migration. We moved forty percent of the customer records so far.",
        "The remaining sixty percent contains the older accounts, which have inconsistent formats.",
        "For example, some phone numbers are stored with spaces and some with dashes.",
        "We wrote a cleanup script, but it has to be reviewed carefully before running on production.",
        "I suggest we run it on a copy first and compare the counts before and after.",
        "Agreed. Nothing runs on production until the comparison matches exactly.",
        "Tomorrow the team will finish the copy, and Dana will run the comparison on Thursday.",
        "If everything matches, the production run will happen on the weekend of the sixteenth.",
    ],
    # ロードマップ
    [
        "Let's spend a few minutes on the roadmap for next quarter.",
        "The sales team keeps asking for single sign-on, and two large customers say it blocks their purchase.",
        "Single sign-on is a big piece of work. I would estimate at least six weeks.",
        "Compared with that, improving onboarding is smaller, but it affects every new customer.",
        "Can we do both, or do we need to choose?",
        "With the current team, I think we can only do one of them well in the quarter.",
        "Then I suggest single sign-on goes first, since there is revenue attached to it.",
        "We can still do a small onboarding improvement, like a better checklist, in the spare time.",
        "All right, the decision is single sign-on first, with a lightweight onboarding checklist alongside.",
        "Priya will draft the plan, and we will review it together at the start of next month.",
    ],
    # 質疑と終わり
    [
        "Before we close, does anyone have questions or anything that we missed today?",
        "I have one. What happens to the November date if the regression finds new problems?",
        "Then we would move the release by one week, and we would tell the customers right away.",
        "Another question about the certificate inventory. Does it include the ones on partner systems?",
        "Only our own systems for now. Partner systems can be a second phase.",
        "Okay, thanks everyone. To summarize, Dana confirms the login fix by Friday.",
        "Luis sends the revised forecast and talks to the monitoring vendor.",
        "Priya updates the job posting, Sam writes the export estimate, and Mei builds the inventory.",
        "Rachel finishes the script for the launch video. Thank you all, have a great week.",
    ],
]

_NUMBER = re.compile(r"\b\d+\b")


def sample_meeting() -> list[str]:
    """約 100 行（107 行）の短い会議の字幕。話された順。"""
    return [line for topic in _TOPICS for line in topic]


def long_meeting(lines: int) -> list[str]:
    """話題を繰り返して、指定の行数にした会議の字幕。話された順。

    繰り返しのたびに、文中の 1 桁から 2 桁の数字を、繰り返しの回数だけずらす（同じ文が並ばないように）。
    """
    base = sample_meeting()
    result: list[str] = []
    round_number = 0
    while len(result) < lines:
        for line in base:
            if len(result) == lines:
                break
            result.append(_shift_numbers(line, round_number))
        round_number += 1
    return result


def _shift_numbers(line: str, amount: int) -> str:
    """文中の数字を amount だけずらす。amount が 0 なら、そのまま返す。"""
    if amount == 0:
        return line
    return _NUMBER.sub(lambda match: str(int(match.group()) + amount), line)
