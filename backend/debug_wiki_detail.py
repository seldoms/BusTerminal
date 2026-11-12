import os


def live_tests_enabled():
    return os.getenv("BUS_TERMINAL_ALLOW_LIVE_TESTS", "").lower() in {"1", "true", "yes", "on"}

def debug_wiki():
    if not live_tests_enabled():
        print("Live Wikipedia debug skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        return

    page_title = os.getenv("BUS_TERMINAL_WIKI_DEBUG_TITLE", "").strip()
    if not page_title:
        print("Live Wikipedia debug skipped. Set BUS_TERMINAL_WIKI_DEBUG_TITLE to the page title.")
        return

    import wikipediaapi

    wiki = wikipediaapi.Wikipedia(
        user_agent='BusTerminal/1.0 (https://github.com)',
        language='ja',
        extract_format=wikipediaapi.ExtractFormat.WIKI
    )

    page = wiki.page(page_title)
    
    print(f"Page Exists: {page.exists()}")
    if page.exists():
        print("Title:", page.title)
        print("Summary:", page.summary[:200])
        print("-" * 20)
        # Check full text availability
        print("Full Text Sample:", page.text[:1000])
        
        # Check sections
        for s in page.sections:
            print(f"Section: {s.title}")
            print(s.text[:200])

if __name__ == "__main__":
    debug_wiki()
