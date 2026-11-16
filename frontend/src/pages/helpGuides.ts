// Guide content for the in-app help center (/help) and the static
// marketing site (site/). Copy lives here once; pages render it.
// Screenshots: frontend/public/help/*.png (served at /help/*.png).

export interface GuideStep {
  heading: string;
  body: string;
  /** Path to screenshot, e.g. "/help/rules.png" (app) or "assets/rules.png" (site). */
  image?: string;
  imageAlt?: string;
  /** Downloadable resources for the step (sample files, templates). */
  links?: { label: string; href: string }[];
}

/** A comparison table rendered under a guide's steps (e.g. the migration
 * gaps table). `rows` cells align with `columns` by index. */
export interface GuideTable {
  caption: string;
  columns: string[];
  rows: string[][];
}

export interface Guide {
  slug: string;
  title: string;
  blurb: string;
  steps: GuideStep[];
  /** Optional tables rendered after the steps, in order. */
  tables?: GuideTable[];
}

export const GUIDES: Guide[] = [
  // --- M89 learning path: the three walkthroughs the quickstart tracks link to.
  // Screenshots are prod captures taken at M89 commit time; docs/09's guide-refresh
  // rule has QA re-capture them when the UI moves underneath them.
  {
    slug: "first-import",
    title: "Your first import, start to finish",
    blurb: "Where to click, what each colour means, and how to handle duplicate rows the first time.",
    steps: [
      {
        heading: "Start at Imports",
        body: "Imports is a row in the left-hand navigation. Everything you bring in runs through the three steps printed on the card \u2014 Upload, Review, Done \u2014 so you always know which one you are in. Nothing has been uploaded on this account yet, which is why the Batches list below the card is empty; that list is the memory of everything you have ever imported, with the status of each one.",
        image: "/help/imports-empty.png",
        imageAlt: "Imports page with an empty Batches list before anything has been uploaded",
        links: [{ label: "Download the sample OFX", href: "/help/import-samples/sample.ofx" }],
      },
      {
        heading: "Choose the file and the account it belongs to",
        body: "Pick the statement your bank exported (CSV, OFX, QFX or QIF) and the account it belongs to. The Import as toggle decides whether the rows are read as bank activity or as brokerage trades. The Preview import button stays disabled until both are chosen \u2014 an import without a destination is one you could not undo.",
        image: "/help/imports-file-chosen.png",
        imageAlt: "Upload form with sample.ofx selected, an account chosen, and the Preview import button enabled",
      },
      {
        heading: "Read the preview before anything exists",
        body: "Preview writes nothing: the header says \u201cPreview — nothing is saved yet\u201d and it means it. Above the rows you can see the column roles the file was read with, so \u201cdate came from DTPOSTED, amount from TRNAMT\u201d is something you can check instead of take on trust. This first pass is the happy path \u2014 four rows, every verdict green, nothing to decide yet.",
        image: "/help/imports-preview-first.png",
        imageAlt: "Preview of four new rows with every verdict green and the column roles listed above the table",
      },
      {
        heading: "The three colours, and what each one means",
        body: "Every row carries a verdict, and the colour is the whole message. Green means new: nothing like it exists and it will import. Amber means near-duplicate: the same amount within three days of something you already have. Red means exact duplicate: that transaction is already in your data, so it is skipped and never written twice. The chips under the header add up to the same story \u2014 in this example, 1 new, 1 exact duplicate, 1 near-duplicate \u2014 and nothing is hidden, because a row that will not import says so in its own column.",
        image: "/help/imports-preview-duplicate.png",
        imageAlt: "Preview with one red exact duplicate, one amber near-duplicate and one green new row",
      },
      {
        heading: "Make the review decision",
        body: "Nothing is written until you press Confirm. Confirm stages the rows into a batch that is still marked Review, and Cancel — import nothing throws the parse away and leaves your data exactly as it was. The note under the buttons is the contract: near-duplicates are never dropped for you, so if the amber row really is a second purchase, it is already in the batch waiting to be committed, and if it is not, you can uncheck it before you commit.",
        image: "/help/imports-review-decision.png",
        imageAlt: "Bottom of the preview with the Confirm import and Cancel buttons and the no-silent-drops note",
      },
      {
        heading: "Commit when you are sure",
        body: "A staged batch needs one more click: \u201cCommit N new transactions\u201d makes the rows live, and balances, budgets and reports all see them from that moment. The toast confirms it and the batch badge turns green in the Batches list. Got it wrong? A committed batch keeps a Revert button that takes its rows back out in one click.",
        image: "/help/imports-committed.png",
        imageAlt: "Batch committed toast, a green Committed badge on the batch, and the Revert button",
      },
      {
        heading: "See it in your feed",
        body: "Open Transactions: the rows you just committed are there with their own dates, next to anything already in the account \u2014 each with an account and a category slot ready to be filled in, or for the suggestion queue to propose one. The Import guides by bank page under Help lists which export to pull from each institution and which date format that bank likes to use.",
        image: "/help/imports-in-transactions.png",
        imageAlt: "Transactions feed showing the imported rows with dates, payees and amounts",
      },
    ],
  },

  {
    slug: "first-budget",
    title: "Your first budget, and what that red bar means",
    blurb: "Set a limit, watch it honestly \u2014 including the week you blow past 100%.",
    steps: [
      {
        heading: "Start one",
        body: "Budgets lives in the TOOLS section of the navigation. Press New budget, pick a category, give it an amount, and choose whether it repeats monthly or yearly \u2014 that is the whole form. Budgets are per category rather than per account, so one number covers every card and account that category appears on.",
        image: "/help/budget-create.png",
        imageAlt: "New budget form with category, amount and period fields",
      },
      {
        heading: "A worked example: $60 for groceries",
        body: "Say you set groceries to $60 a month, then the month goes sideways and you spend $123.45 of it. The card reads $123 / $60 and prints 206% used: the true number, rounded to a whole per cent, not a bar that quietly stops at the top. The bar fill caps at 100% because a bar cannot be longer than its card, but the number beside it never caps. If you are at 206%, you are told 206%.",
        image: "/help/budgets-over-100.png",
        imageAlt: "Groceries budget showing $123 spent against a $60 monthly limit and 206% used",
      },
      {
        heading: "What the colours tell you",
        body: "Green means you are under 80% of the limit. Amber means 80% to 99% \u2014 still inside, but the rest of the month is going to be tight. Red means 100% or more: the money is spent, and the number says by how much. The colour is never the only signal either \u2014 the percentage sits right next to the bar as text, so it survives a colour-vision difference, a screenshot and a bad monitor.",
      },
      {
        heading: "What hovering gives you",
        body: "Hovering the icons on the right of a budget row gives you their names: Edit (the pencil) and Delete (the bin). The percentage itself is not behind a hover \u2014 it is printed on the card at all times, which is what makes the over-budget moment impossible to miss on the way past.",
      },
      {
        heading: "Categories that work well",
        body: "Canadian household budgets land well on three or four categories you can actually name: groceries (weekly and volatile \u2014 the one in the example above), entertainment (dining and streaming together, so one limit covers both), and housing (rent or mortgage plus utilities, the number that never moves). Start with those three. A budget with twenty categories is a budget you stop updating in a fortnight.",
      },
    ],
  },

  {
    slug: "agent-tour",
    title: "Your agent did this — watch it happen",
    blurb: "You ask, your agent calls a tool, a receipt lands. A real run, step by step.",
    steps: [
      {
        heading: "What you asked for",
        body: "Your agent — Claude, Cursor, or a script you wrote — has been handed a token you minted yourself, and you asked it a plain question about your own data. From here on there is no screen involved: everything below happened between your agent and your fin_tracker, and it was recorded whether or not you were watching.",
      },
      {
        heading: "What your agent actually called",
        body: "The session opens, then the agent asks what it is allowed to do, and tools/list answers with 52 tools — every one of them read-only, because a read-only token is never shown the write tools at all; they are not in the list to begin with. A read then goes through (health_check, healthy, 73 ms), and an attempted write (accounts_create) gets 403: this token is read-only. That refusal is structural rather than polite — the same token cannot get a different answer on a second attempt.",
        image: "/help/agent-run.png",
        imageAlt: "Transcript of a session in which the tools list is read-only, one read succeeds and one write is refused with 403",
      },
      {
        heading: "The receipt it left behind",
        body: "Every call writes one row: which tool, what it answered, how long it took, and which token carried it. Two calls, two receipts, newest first — health_check at 200 in 73 ms, accounts_create at 403 in 0 ms. Arguments are hashed rather than stored, so a receipt can never leak a password you handed your agent, and when you revoke the token the rows it produced stay with your account.",
        image: "/help/agent-receipts.png",
        imageAlt: "Receipts listing two rows: health_check 200 in 73 ms and accounts_create 403 in 0 ms",
      },
      {
        heading: "Do it against your own data",
        body: "Mint your own token, point your client at the MCP endpoint, and ask it something real. The Drive it from your agent guide under Help explains what each policy allows before you hand one over, and Configuration (MCP) has the exact request shapes if you would rather read them first.",
      },
    ],
  },
  {
    slug: "getting-started",
    title: "Getting started",
    blurb: "Log in, try the demo, and read your dashboard in 2 minutes.",
    steps: [
      {
        heading: "Try the demo first",
        body: "On the login screen, press “Explore the demo”. It loads a fully seeded household — accounts, transactions, rules, investments — so you can click through every page before entering a cent of your own data. Nothing you do in the demo touches a real account.",
      },
      {
        heading: "Read the dashboard",
        body: "The dashboard opens with Net worth (assets minus liabilities — cash across every account plus holdings at their latest cached price, with credit cards and lines of credit counting as negative cash; the ? on the label shows both numbers and what is excluded), Range net (income minus spending summed over every month bucket in the selected range), and Spending insights below. The 1W / 1M / 3M / 6M / YTD / 1Y buttons — or the From/To dates — re-scope every number on the page.",
        image: "/help/dashboard.png",
        imageAlt: "Dashboard with net-worth KPIs, date-range controls, and spending insights",
      },
      {
        heading: "Move around",
        body: "The sidebar holds the main sections: Dashboard, Transactions, Imports, Investments, Reports, Taxes, Settings — plus Tools (Currencies, Tags, Rules, Budgets, Goals, Bills, Debts). The search box at the top finds transactions, accounts, and tags from anywhere.",
      },
    ],
  },
  {
    slug: "transactions",
    title: "Transactions",
    blurb: "Filter, edit, tag, and confirm suggestions on your money feed.",
    steps: [
      {
        heading: "Find anything with the filter bar",
        body: "Filter by text, category, account, date range, or tag. Clicking a tag anywhere in the app (a transaction row, the Rules page, search results) deep-links here with the tag pre-applied, so tags double as saved views.",
        image: "/help/transactions.png",
        imageAlt: "Transactions page with filter bar and suggestion queue",
      },
      {
        heading: "One category, many tags",
        body: "Every transaction has exactly one category — that is what budgets and reports add up. Tags are extra labels and you can attach several (e.g. a flight tagged travel and reimbursable). Create tags inline by typing a new name; it is created the moment you attach it.",
      },
      {
        heading: "Work the suggestion queue",
        body: "The Suggestions panel proposes categories from three sources in order: your rules first, then learned payees (what you confirmed before), then the assistant. Press Run to generate a batch, confirm what is right, reject what is not — every confirmation teaches the app your preference.",
      },
    ],
  },
  {
    slug: "rules",
    title: "Rules & automation",
    blurb: "File transactions automatically, preview safely, backfill history.",
    steps: [
      {
        heading: "Create a rule",
        body: "Pick a match type (contains a keyword, exact payee, or regex), type the pattern (e.g. NETFLIX), set priority, and choose the category plus optional tags. Rules run lowest priority number first. Keep broad rules at high numbers (100+) and specific exceptions at low numbers (10–20).",
        image: "/help/rules.png",
        imageAlt: "Categorization rules editor with pattern, priority, and stop toggle",
      },
      {
        heading: "Stop vs continue",
        body: "“Stop here on match” (on by default) files the transaction and skips later rules — the classic first-match-wins. Turn it off and processing continues: each later match applies in order, so the last matching rule's category wins and tags accumulate. Continuing rules show a “continues” badge in the table.",
      },
      {
        heading: "Test run before you trust it",
        body: "Press the magnifier on any rule for a dry-run: it shows exactly which transactions would match, and it never writes anything. When satisfied, Apply with a scope — Forward only for new transactions from now on, All history, or a Date range — plus Only-unassigned to skip already-categorized rows and Overwrite to re-file them.",
      },
    ],
  },
  {
    slug: "imports",
    title: "Imports",
    blurb: "Upload bank statements safely: staged, reviewed, then committed.",
    steps: [
      {
        heading: "Upload a statement",
        body: "Head to Imports → Upload a statement and drop in your bank or brokerage CSV. Map the columns once (date, description, amount); the mapping is remembered per institution next time.",
        image: "/help/imports.png",
        imageAlt: "Imports page with upload flow and staged batches",
      },
      {
        heading: "Review the staging batch",
        body: "Imports land in a staging batch first — nothing counts toward balances, budgets, or reports until you commit. Review the parsed rows, fix anything odd, then commit the batch to make it official.",
      },
      {
        heading: "Duplicates are blocked",
        body: "Re-importing the same file will not create duplicates: dedup spans pending and committed batches. If a file is rejected, the error names the exact row and reason (bad date, bad amount, unknown column) so you can fix and retry.",
      },
      {
        heading: "Move over a YNAB budget",
        body: "Coming from YNAB? The Migrate from YNAB guide walks through both exports (a full-budget JSON straight from YNAB's API, or one account's register CSV) and lists exactly what carries over and what does not. Short version: the preview maps every account, category, group tag, and target, and lists each row it cannot import with its row number and reason — nothing is dropped silently. Accounts, categories, and tags are matched by name, so running the import a second time adds nothing, and every transaction lands in a review batch you commit yourself.",
      },
    ],
  },
  {
    slug: "migrate-from-ynab",
    title: "Migrate from YNAB",
    blurb: "Export your budget, review the whole mapping before anything is written, then import — refused rows are listed, never dropped.",
    steps: [
      {
        heading: "Export from YNAB",
        body: "Whole budget (recommended): in YNAB open Settings → Developer and create a personal access token, then save the response of GET https://api.ynab.com/v1/plans/last-used (Authorization: Bearer your-token) to a file ending in .json — that one call is YNAB's own full-plan export, carrying accounts, category groups, categories, payees, and every transaction. One account at a time: use the account's Export option to download its register as CSV — no token needed, but the file has no category tree and no targets.",
      },
      {
        heading: "Preview the whole plan first",
        body: "Imports → Import a YNAB budget → choose the file → Preview. Nothing is written: you see every account that will be created or matched by name, each category group becoming a top-level category with the group name attached as a tag, each target as a budget or goal, and every transaction with its date, amount, memo, and cleared state — plus each row the import refuses, carrying its row number and the reason.",
      },
      {
        heading: "Import, then commit batch by batch",
        body: "Press Import and each account's transactions land in their own review batch, exactly like a bank statement upload: balances, budgets, and reports do not move until you commit a batch. Transfers arrive as linked pairs, and YNAB's cleared / reconciled flags stay on every row.",
      },
      {
        heading: "Safe to run twice",
        body: "Accounts, categories, and tags match existing ones by name, and transactions dedup against staged and committed rows alike — importing the same file again stages nothing new, and the preview tells you so before you try.",
      },
      {
        heading: "Check the two tables below",
        body: "What maps cleanly, and what does not come across — read them before you cancel anything on the other side.",
      },
    ],
    tables: [
      {
        caption: "What maps cleanly",
        columns: ["In YNAB", "Here"],
        rows: [
          [
            "Accounts — chequing, savings, credit cards, loans",
            "Matched by name when you already have one, otherwise created with the closest account type. Taxability is derived the same way as a hand-made account.",
          ],
          [
            "Category groups",
            "Become top-level categories, and the group name is attached as a tag on that group's transactions.",
          ],
          [
            "Categories",
            "Become child categories under their group, so the two-level tree survives.",
          ],
          [
            "Targets — monthly funding",
            "Become a monthly budget on that category. An existing budget amount is never overwritten.",
          ],
          [
            "Targets — target balance, with or without a date",
            "Become a goal with the amount and date.",
          ],
          [
            "Transactions",
            "Every row with its date, amount (YNAB's milliunits converted to dollars and cents), payee, and memo folded into the description.",
          ],
          [
            "Cleared and reconciled flags",
            "Kept on each row (uncleared / cleared / reconciled).",
          ],
          [
            "Transfers between your accounts",
            "Both sides imported and linked as one transfer pair.",
          ],
        ],
      },
      {
        caption: "What does not come across",
        columns: ["In YNAB", "What happens here"],
        rows: [
          [
            "Split detail",
            "The parent total imports as one row; the split lines are reported as a warning instead of being imported.",
          ],
          [
            "Scheduled transactions",
            "Skipped — reported once as a note on the preview.",
          ],
          [
            "Targets other than monthly funding or target balance (needed-for-spending, debt)",
            "Refused with a reason in the preview, never silently skipped.",
          ],
          [
            "Reconciliation progress and account running balances",
            "Not imported. The cleared / reconciled flag on each individual row still comes across.",
          ],
          [
            "Group funding and underfunded amounts",
            "Not imported — budgets and goals are yardsticks here, and nothing is moved or auto-spent.",
          ],
          [
            "Payee-to-category memories (YNAB's learned rules)",
            "Not imported; payee learning starts fresh from the suggestions you confirm.",
          ],
          ["Closed accounts", "Imported as open accounts — the preview notes it."],
          [
            "Deleted accounts, categories, and transactions",
            "Skipped and reported in the preview notes.",
          ],
          [
            "Account opening balances or a partial export",
            "Balances rebuild from the transactions in the file, so a trimmed export shows different balances than YNAB did.",
          ],
          [
            "A budget on a tenant with encryption required",
            "The import stops with a clear error before writing anything — the server cannot create your encrypted rows. Manual entry with client-side encryption still works.",
          ],
        ],
      },
    ],
  },
  {
    slug: "reports",
    title: "Reports & budgets",
    blurb: "Spending insights, auto-detected subscriptions, monthly budgets.",
    steps: [
      {
        heading: "Spending insights",
        body: "Reports breaks spending down by category and month so you can see where the money actually goes. Change the date range to compare periods; every figure drills from the same transaction feed, so reports always agree with Transactions.",
        image: "/help/reports.png",
        imageAlt: "Reports page with spending breakdown charts",
      },
      {
        heading: "Subscriptions find themselves",
        body: "Recurring payments are detected automatically from payees charged 2 or more times — there is nothing to configure. Each shows frequency, average amount, and projected monthly/yearly cost. To stop tracking one, remove or rename the underlying payee rather than looking for a delete button.",
      },
      {
        heading: "Budgets track one category per month",
        body: "Create a budget per category with a monthly amount; cards show spent-vs-allowed at a glance. Editing a budget amount never moves transactions — it only changes the yardstick.",
        image: "/help/budgets.png",
        imageAlt: "Budgets page with monthly category cards",
      },
    ],
  },
  {
    slug: "taxes",
    title: "Taxes (Canada)",
    blurb: "ACB-tracked capital gains and CRA tax projection.",
    steps: [
      {
        heading: "Gains use average cost (ACB)",
        body: "Canadian rules require the adjusted-cost-base method: buys raise your cost base per holding, sells realize gains against it. The Taxes page shows the gain per year and the ACB pool per holding in non-registered accounts — recomputed from your transactions, never hand-edited.",
        image: "/help/taxes.png",
        imageAlt: "Taxes page with capital-gains summary and ACB per holding",
      },
      {
        heading: "Keep buys and sells accurate",
        body: "The pools rebuild from your non-registered trade history, so accurate buys, sells, and corporate actions are all it takes. If numbers look stale after edits, use Rebuild — the single sanctioned write path for cost pools.",
      },
      {
        heading: "Project next year's bill",
        body: "Tax Projection estimates federal + provincial (ON, BC, AB) tax from employment, capital-gains, and dividend inputs with deductions — showing taxable income, tax amounts, and average/marginal rates for planning, not filing.",
      },
    ],
  },
  {
    slug: "portability",
    title: "Export & portability",
    blurb: "Exactly what you can take with you — the file list, the schema, and the no-lock-in promise.",
    steps: [
      {
        heading: "One click, one ZIP",
        body: "Settings → Privacy → Export my data → Download ZIP gives you fin-tracker-export-<date>.zip: a manifest.json plus one CSV per table — accounts, transactions, categories, tags, rules, trades, budgets, goals, bills, debts, subscriptions, prices, fx rates and every other table the app keeps. The list is generated from the live schema, not a hand-written one, so a table added later appears in your next export without anyone remembering to update a list.",
        image: "/help/portability.png",
        imageAlt: "Settings → Privacy & data with Download ZIP, Download JSON and the OFX account picker",
      },
      {
        heading: "The file documents itself",
        body: "manifest.json is the schema of everything else in the archive: every file with its row count and exact column order, plus the export's version, timestamp, vault state, and the tables that were deliberately left out (with the reason). CSVs are UTF-8 with a single header row: an empty cell is null, booleans are true / false, dates are YYYY-MM-DD, and money is an exact decimal string — never a float that rounds your balances.",
      },
      {
        heading: "No lock-in — the whole point",
        body: "Plain CSV and JSON in a plain ZIP. No proprietary container, no export key, no second account, nothing to unlock and nothing to re-import before you can read it. Open it in a spreadsheet, load it into SQLite, or hand it to another app. The panic this answers is the familiar one — “download your history or you will never have access again” — and the answer is not a reassurance, it is this button: your complete history in a format that outlives any one product, including this one.",
      },
      {
        heading: "Round-trip into other tools with OFX",
        body: "Each account also exports as an OFX 1.02 statement (Settings → Privacy → Export my data → pick an account → Download OFX). OFX is the interchange format banks and finance tools already speak, and our own importer reads it: feeding the file straight back into fin_tracker imports 0 new rows, because deduplication recognizes every transaction already in your ledger.",
      },
      {
        heading: "Schedule it, and watch the committed-only rule",
        body: "Scheduled export stores a ZIP in this browser — daily, weekly or monthly, keeping the last 1–20 archives — using the same schedule shape as the scheduled backup. It needs a tab open, like every client-side schedule, and the archives live only on this machine: clearing browser data removes them, so download the ones you want to keep. The ZIP contains what the app shows you — committed transactions and trades only; rows still sitting in a pending import are left out until you confirm them. The full machine snapshot (staged rows included) is the separate Download JSON button.",
      },
    ],
  },
  {
    slug: "import-guides",
    title: "Import guides by bank",
    blurb: "Where each Canadian institution keeps its export, which file to pick, and the date quirks that bite.",
    steps: [
      {
        heading: "Pick the format before you pick the file",
        body: "Three formats reach the Imports page. OFX and QFX are the same thing — QFX is just Quicken's name on OFX — and both are detected automatically with no column mapping, so grab whichever your bank offers. QIF is the older line-based format and is also detected on its own. CSV always needs its columns mapped once per institution; the wizard remembers the mapping, so it costs you one pass per bank and never again. Nothing is sent anywhere: the file is read by the server you host, which does not call out to a third party.",
      },
      {
        heading: "Preview first, Confirm second",
        body: "Press Preview import and the whole file is parsed without writing a row. You get the column roles it used, every row's verdict — new, exact duplicate or near-duplicate — and the CAD amount produced from the cached exchange rate for that date. Confirm stores the batch for review; Cancel leaves nothing behind, so it is safe to preview a file you are not sure about.",
      },
      {
        heading: "How duplicates are decided",
        body: "An exact duplicate matches something you already have on account, date, amount and description. A near-duplicate carries the same amount within three days with a payee close enough to be the same merchant — the preview names the matching row, how many days apart it is, and whether that row is already imported or still waiting in another import. Near-duplicates are imported and stay flagged in the batch: nothing is ever dropped for you. If you decide one is a repeat, discard the batch, or fix the row after you commit.",
      },
      {
        heading: "Sample files, no real money in them",
        body: "Four synthetic statements, one per format, with invented merchants and amounts. Run one through Preview first if you want to see the verdicts before pointing the importer at a real export.",
        links: [
          { label: "Sample CSV", href: "/help/import-samples/sample.csv" },
          { label: "Sample OFX", href: "/help/import-samples/sample.ofx" },
          { label: "Sample QFX", href: "/help/import-samples/sample.qfx" },
          { label: "Sample QIF", href: "/help/import-samples/sample.qif" },
        ],
      },
      {
        heading: "Date formats are where imports go wrong",
        body: "QIF files carry no locale, so a date like 05/06/2026 is read month-first — March-style order, the same convention Quicken used — unless one of the numbers is over 12, in which case the data settles it. In CSV the date order is yours to set: the wizard's Date format dropdown does the work, and the preview shows the parsed date on every row before you commit, so a swapped day and month is visible as a weird-looking date rather than a silently wrong one. Watch the first row of any preview; if it looks like last month instead of this month, change the format and preview again.",
      },
      {
        heading: "RBC Royal Bank",
        body: "Where: sign in, open the account, and look for a Download account history control on the account activity page — credit cards use the same control from the card's activity view. Format: RBC offers CSV as well as QFX; take the QFX when it is there. Quirk: RBC's CSV can split money into separate withdrawal and deposit columns, so choose Debit & Credit in the mapping wizard instead of a single amount — the sign then takes care of itself.",
      },
      {
        heading: "TD Canada Trust (EasyWeb)",
        body: "Where: Accounts, open the account, then Download or Export from the account activity list. Format: QFX for chequing, savings and most cards, CSV everywhere. Quirk: card exports sometimes list every charge as a positive number; the Credits positive switch in the mapping wizard flips the whole file to the right sign in one click.",
      },
      {
        heading: "BMO (Bank of Montreal)",
        body: "Where: Accounts, open the account, then Download from the account activity page. Format: CSV and QFX. Quirk: BMO CSVs tend to leave the description column carrying reference text alongside the payee; the preview shows it verbatim, and you can clean any row up after you commit rather than editing the export.",
      },
      {
        heading: "Scotiabank",
        body: "Where: Accounts, then Account history, then Download. Format: CSV and QFX. Quirk: Scotiabank's site shows dates one way while some exports write them another — set Date format in the wizard to match the file itself, not the page you downloaded it from, and confirm on the preview's first row.",
      },
      {
        heading: "Tangerine",
        body: "Where: Accounts, open the account, then look for Download under Recent activity. Format: CSV and QFX. Quirk: Tangerine's Chequing and Savings exports are straightforward; credit-card activity exports separately, so download the card statement rather than mixing the two into one file.",
      },
      {
        heading: "Wealthsimple",
        body: "Where: the web app (the phone app does not export), open the account, then use the download control on its Transactions list. Format: CSV. Quirk: investment activity is a different file from Cash activity. Investment trades belong on the Import mode switch set to Investment trades, where the activity columns, symbol and quantity are read; bank-style CSVs go through the normal path.",
      },
      {
        heading: "EQ Bank",
        body: "Where: open the account in online banking and look for a Download or Export control. Format: CSV when offered. Known gap: EQ Bank has been one of the few Canadian banks whose portal only publishes PDF statements, which this importer does not read — if your account shows no CSV or QFX option, there is no file to import yet and the statement cannot be converted. Everything else in these guides applies the moment an export appears.",
      },
      {
        heading: "Rogers Bank",
        body: "Where: sign in to the card account and look for Download under account activity or statements. Format: CSV for card activity. Quirk: Rogers Bank is credit-card only, so there is never a chequing file to worry about; charges and refunds share one signed amount column, and refunds come through positive.",
      },
      {
        heading: "MBNA",
        body: "Where: sign in, open the card account, then Download from the account history or statements area. Format: CSV and QFX. Quirk: MBNA runs on TD's platform, so the layout follows EasyWeb — the same Credits positive switch applies if charges arrive unsigned.",
      },
      {
        heading: "Credit unions (Desjardins, Vancity, Coast Capital, Meridian, and the rest)",
        body: "Where: every credit union runs one of a handful of online-banking platforms, and they all expose a download on the account activity page — the label is usually Download, Export or Save as. Format: CSV is universal; OFX or QFX is offered on most platforms but not all. Quirk: because the platform is shared across unions, the mapping you set up for one institution usually carries over to another on the same system, so a second credit union often needs no setup at all.",
      },
      {
        heading: "When a file will not import",
        body: "The error names the exact reason rather than failing silently: an unrecognized file means the export is not one of the three formats; a parse error names the record that could not be read (a missing date or amount stops the file instead of dropping the row); a missing mapping means the CSV has no saved column map yet, and the wizard opens so you can set one. Nothing partially imports — a rejected file leaves the database exactly as it was.",
      },
    ],
  },

  {
    slug: "agent-integration",
    title: "Drive it from your AI agent",
    blurb: "Connect ChatGPT, Claude, Codex, Cursor, or your own script to your own fin_tracker — a scoped token, a read-only mode, a confirm-before-change propose mode, and a receipt for every call.",
    steps: [
      {
        heading: "Mint a token in one call",
        body: "Sign in the way you normally would, then POST to /api/v1/auth/mcp-tokens with a name and a policy — for example {\"name\":\"laptop-agent\",\"policy\":\"read_only\"}. The response carries an opaque 64-character token that is shown exactly once and stored only as a hash; it expires 90 days after you create it. Omit policy and you get full — the behaviour every token had before policies existed. The demo tenant runs the same recipe with one rule: its tokens are always read-only, so an omitted policy gives you read_only and an explicit \"full\" or \"read_propose\" is refused with 422 code demo_mcp_read_only instead of being quietly downgraded. Revoke any token at any time with DELETE /api/v1/auth/mcp-tokens/{id} — the demo tenant can revoke its own tokens too.",
      },
      {
        heading: "Point your client at /mcp/mcp",
        body: "The Model Context Protocol endpoint lives on the same host as the app, one path deeper: https://your-server/mcp/mcp. Add it to your client as a remote server with your token as a bearer header — {\"mcpServers\":{\"fin_tracker\":{\"url\":\"https://your-server/mcp/mcp\",\"headers\":{\"Authorization\":\"Bearer <your-token>\"}}}}. On the same machine you can run the server in stdio mode instead (python -m mcp_server from the backend directory) and it exposes the same tools over stdin/stdout.",
      },
      {
        heading: "A read-only token cannot write — by construction",
        body: "Connect with a read-only token and the client lists 53 tools instead of 107: every mutating tool is removed from the tools/list response rather than merely flagged, so an agent that has never seen accounts_create cannot call it. If a client that cached the full list tries anyway, the server answers 403 with code mcp_read_only before anything leaves the process — no request reaches the app, no credential is issued. The demo tenant gets the same protection from the other side: its list is filtered the same way, each tool carries the title marker \u201cdemo tenant is read-only\u201d, and its refusals come back with the app's own demo_account_restricted code.",
      },
      {
        heading: "Bring your own AI: read and propose, nothing more",
        body: "This is the mode for ChatGPT, Claude or Codex: mint the token with policy read_propose — {\"name\":\"chatgpt\",\"policy\":\"read_propose\"}. The client then lists 54 tools: the 53 reads plus proposals_create, the one write this token may make. Ask your AI to change something and it calls proposals_create, which runs the same validation the in-app assistant uses and lands a pending card — nothing in your data moves at that point. Any other mutating tool is answered with 403 and code mcp_read_propose at the MCP layer before a request is made, telling the agent to propose the change instead. The demo tenant cannot mint this policy: an explicit read_propose is refused with 422 demo_mcp_read_only, never quietly downgraded.",
      },
      {
        heading: "Only you can confirm a proposal",
        body: "There is deliberately no tool to apply, dismiss, undo or list proposals from the connector side — in any policy tier — so an external session can never confirm its own suggestion. The pending card appears in the app with its Now/With-change columns: Apply writes the change and an audit row in the same transaction, Dismiss closes it without touching anything, and History carries an Undo that restores the previous values exactly. Every apply and undo is recorded with its before/after values, and the receipts in GET /api/v1/mcp-audit show which token proposed it and when.",
      },
      {
        heading: "Your data stays between you and your AI",
        body: "fintrack makes no outbound calls for this feature — your AI app connects to fintrack, asks, and gets answers over that same connection. The only party that receives your data is the AI provider you chose (OpenAI, Anthropic, …), because that is who you asked to read it. Nothing else leaves this machine.",
      },
      {
        heading: "Every call leaves a receipt",
        body: "GET /api/v1/mcp-audit returns your own rows, newest first: which tool ran, the response code, how long it took, which token it came through, and a 64-character sha256 of the arguments. Arguments are hashed because a login call carries a password — the receipt answers \u201cwhat did the agent touch?\u201d without keeping the secret itself. Refusals are recorded too: a rejected write shows up with code 403, exactly like a successful one shows 200.",
      },
      {
        heading: "Read what the agent did, then revoke it",
        body: "The trail is yours alone — it is scoped to your account the same way everything else is, and a different login sees none of your rows. Revoking the token kills access immediately (the next request comes back 401 with the body {\"error\":\"Invalid MCP token\"} — the machine-readable code invalid_mcp_token exists only on the internal validate endpoint, which the reverse proxy answers 403 to) while the rows it produced stay put: the log belongs to the account, not to the credential, so cleaning up tokens never erases the history you may want to check.",
      },
    ],
    tables: [
      {
        caption: "What one receipt tells you",
        columns: ["Field", "Meaning"],
        rows: [
          ["tool", "The MCP tool that was called — accounts_create, transactions_delete, and so on."],
          ["response_code", "200 for a success, 403 when the server refused it, the app's own status when the API rejected it, 500 for an unexpected failure."],
          ["duration_ms", "Wall-clock time of the whole call, measured at the MCP server."],
          ["mcp_token_id", "Which token carried it — empty when the call arrived without one, as a local stdio session does."],
          ["args_hash", "sha256 of the arguments, so identical calls match and secrets never land in the table."],
          ["created_at", "When it happened."],
        ],
      },
    ],
  },
  {
    slug: "configuration",
    title: "Configuration (MCP)",
    blurb: "The endpoints, payload shapes, and policies behind agent access — the reference page for humans and their agents.",
    steps: [
      {
        heading: "Three endpoints, one secret",
        body: "You mint and revoke tokens against /api/v1/auth/mcp-tokens with your normal login. The MCP server itself validates the bearer token at /api/v1/auth/validate-mcp-token, and it posts each receipt to /api/v1/auth/internal-mcp-audit — those two are internal, denied at the reverse proxy, and reachable only from the compose network. You read receipts at /api/v1/mcp-audit. Nothing about this needs configuration beyond the three variables the MCP container runs on.",
      },
      {
        heading: "Full, read-only, or read + propose",
        body: "The policy is set when the token is created and cannot be changed afterwards — mint a new one instead. A full token sees all 107 tools. A read_propose token (the bring-your-own-AI connector) sees 54 — the 53 reads plus proposals_create — and any other mutating call is refused with 403 code mcp_read_propose. The demo tenant never gets one: its mints are forced to read_only (an omitted policy resolves to read_only, an explicit full or read_propose is refused with 422 demo_mcp_read_only), so a demo session always lists the 53. A read-only token sees the 53 that only read, and any attempt to call a mutating tool is answered with 403 and code mcp_read_only at the MCP layer, before a request is made. Existing tokens keep the policy they were created with, so nothing changes for a deployment that never asked for read-only.",
      },
      {
        heading: "The three environment variables",
        body: "FIN_TRACKER_URL is where the MCP server finds the API (http://<host>:8000 — the API's address as the MCP container reaches it, never a public URL), MCP_INTERNAL_SECRET is the shared secret it uses to ask for a per-user session and to post receipts, and MCP_UPLOAD_DIR is the directory file imports are allowed to read — paths outside it are refused. The container holds no database credentials and no login secret on purpose: it only ever speaks HTTP to the API.",
      },
      {
        heading: "What a receipt code means",
        body: "Codes are derived, never guessed: a refusal decided at the MCP layer is 403, a status the API itself returned is passed through as-is, a tool error that came back as the app's structured error is 400, and an unexpected failure is 500. Everything else is 200. Receipts are best effort by design — if the API is unreachable when a call finishes, the tool result is still returned and the gap is logged rather than blocking your work.",
      },
      {
        heading: "Counts worth knowing",
        body: "107 tools in total: 53 read and 54 write, where the read set is every GET plus seven POSTs that only compute or manage a session (logging in, refreshing, logging out, the demo switch, running a projection, testing the model connection, and suggesting an import profile). The split is derived from the tool code itself, so a tool added later is classified when it lands rather than when someone remembers to update a list.",
      },
    ],
    tables: [
      {
        caption: "Endpoints",
        columns: ["Call", "Needs", "Returns"],
        rows: [
          ["POST /api/v1/auth/mcp-tokens — {\"name\":\"agent\",\"policy\":\"read_only\"}", "Your normal login", "201 with the token, shown once, plus id, policy, created_at, expires_at. On the demo tenant the policy is forced to read_only (omitted policy → read_only; \"full\" → 422 demo_mcp_read_only)"],
          ["GET /api/v1/auth/mcp-tokens", "Your normal login", "200 with every token you own: id, name, policy, created_at, expires_at, last_used_at — never the token itself"],
          ["DELETE /api/v1/auth/mcp-tokens/{id}", "Your normal login", "204; the token stops working on its next request"],
          ["GET /api/v1/mcp-audit?limit=50&offset=0", "Your normal login", "200 with your newest receipts plus the total"],
          ["POST /api/v1/auth/validate-mcp-token", "A minted MCP token", "user_id, email, policy, token_id, is_demo — the identity the server acts as"],
        ],
      },
      {
        caption: "Policies",
        columns: ["Policy", "Tools listed", "A mutating call"],
        rows: [
          ["full", "All 107", "Proxied to the app under your own account"],
          ["read_propose (bring your own AI)", "The 53 read tools plus proposals_create (54)", "proposals_create lands a pending card only; anything else is refused with 403 code mcp_read_propose, no request made"],
          ["read_only", "The 53 read tools only", "Refused at the MCP layer: 403, code mcp_read_only, no request made"],
          ["demo tenant (its tokens are always read_only)", "The 53 read tools only", "Refused with code demo_account_restricted — the same code the app itself returns"],
        ],
      },
    ],
  },
  {
    slug: "brokerage-imports",
    title: "Brokerage imports",
    blurb: "Where each broker keeps its activity export, which file to take, and exactly what the importer refuses.",
    steps: [
      {
        heading: "Export activity, not positions",
        body: "Every one of these profiles wants a transaction file — one row per trade, dividend, deposit or fee — not a holdings snapshot. Positions files carry a balance with no history, so there is nothing to reconcile against. Take the widest date range you have not imported yet: re-reading the same file a second time stages nothing new, because duplicates are decided on account, security, date, action, quantity and price.",
      },
      {
        heading: "Pick the broker before you upload",
        body: "Imports → choose an investment account → Import as “Investment trades” → Broker export. Leaving it on auto-detect matches the file header against every profile; choosing the institution pins the parser and refuses a file that does not match that broker's export instead of force-fitting it. Either way you get the same staged batch: review the rows, then commit — nothing counts toward balances or reports until you do.",
      },
      {
        heading: "Questrade",
        body: "Where: Reports → Account Activity → all activity types → Download (CSV). Take the whole activity list, not a trades-only slice, so deposits and dividends land in the same file. Quirk: the export carries both Transaction Date and Settlement Date; only the trade date is used, and the settlement column is reported as read-and-ignored rather than silently feeding a calculation.",
      },
      {
        heading: "Wealthsimple",
        body: "Where: the account's Activity list → Download activities → CSV. Quirk: express orders are labelled “Buy - Express” / “Sell - Express” in the file. Those labels are recognized, along with DRIP sub-types; anything Wealthsimple writes that is not in the profile's vocabulary comes back as a listed row with its line number rather than vanishing.",
      },
      {
        heading: "Interactive Brokers (and IBKR Canada)",
        body: "Where: Performance & Reports → Flex Queries → Activity Flex Query, and set Format = CSV — the two supported layouts are the flat CSV export and the sectioned Activity Statement, which is read block by block. Quirk: every row states its own CurrencyPrimary, so one file can hold CAD and USD rows side by side and each is treated in its own currency. Canadian accounts export the identical layout; only CurrencyPrimary reads CAD. Leave the Corporate Actions section out of the routine query — see “What the importer refuses” below.",
      },
      {
        heading: "Fidelity (US)",
        body: "Where: Accounts & Trade → Activity & Orders → Download CSV. Quirk: the file states no currency at all, so every row is USD no matter which account you import it into — including a CAD account, where the conversion then runs through your cached exchange rates. Commission and fees arrive in two separate columns and are added together.",
      },
      {
        heading: "TD Direct Investing",
        body: "Where: WebBroker → the account → Activity → Export (CSV). Quirk: the CAD and USD sides of an account are separate exports, so export both and import each against its matching account — nothing here merges them. Quantity notation such as 1E+1 is read as 10, and quoted descriptions that wrap across two lines stay one row.",
      },
      {
        heading: "RBC Direct Investing",
        body: "Where: My Portfolio → Account Activity → Download (CSV). Quirk: the export has no commission column, so trades import with commission 0.00. That is stated as a notice on the preview — check the rows against your statement rather than assuming the broker charged nothing.",
      },
      {
        heading: "National Bank Direct Brokerage",
        body: "Where: Account → Transaction History → Download (CSV). Quirk: NBDB publishes the same file in English or French. The French columns (Achat, Vente, Dividende, Dépôt, Quantité, Prix, Devise) and the semicolon separators are read as-is, and rows with no currency take the currency of the account you import into.",
      },
      {
        heading: "What the importer refuses",
        body: "Two things stop the whole file rather than a row. A date or a number that cannot be read: importing the rows around it would leave a history that looks complete and is not, so the file is refused with the line number and the formats that would work. A split, merger or reorganization: no supported export states the share count an adjustment needs, so the row is refused with a pointer to entering it by hand instead of being turned into a wrong position. Everything else that cannot become a trade is listed with its line number, the raw activity word and the institution — never dropped in silence.",
      },
    ],
    tables: [
      {
        caption: "What is never guessed",
        columns: ["Situation", "What you get"],
        rows: [
          ["A date in an order the profile does not accept (05/06/2026)", "The row is listed with its line number and the date formats that would work — never a silently swapped day and month. No Canadian profile reads a slash date at all, because the ambiguity cannot be resolved from the data."],
          ["A quantity, price or amount that is not a number", "The whole file is refused with the line number, instead of importing the rows around it."],
          ["A split, merger or reorganization in the export", "The whole file is refused with the line number and the activity word. Add the adjustment by hand on the Investments page, then re-import with that activity excluded."],
          ["An activity word the broker uses but the profile does not", "A listed row carrying its line number, the raw word and the institution. It is never turned into a guess."],
          ["A currency the file does not state", "A per-broker rule: Fidelity rows are always USD; NBDB and RBC blank cells take the account's currency; every other profile requires the cell to be a real three-letter code."],
          ["The same file imported twice", "The second run stages the rows as duplicates and imports nothing new. Duplicates stay in the batch for you to review and can be included one by one."],
        ],
      },
    ],
  },

];

export function getGuide(slug: string | undefined): Guide | undefined {
  return GUIDES.find((g) => g.slug === slug);
}