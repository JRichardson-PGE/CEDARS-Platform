def test_robots_txt_keeps_crawlers_off_the_record_queries(client, settings):
    settings.ROBOTS_DISALLOW_ALL = False
    body = client.get("/robots.txt").content.decode()
    assert body.startswith("User-agent: *\n") and "Disallow: /data/records/\n" in body and "Disallow: /data/download\n" in body
    assert "Disallow: /\n" not in body

    settings.ROBOTS_DISALLOW_ALL = True  # the sandbox and demos: test data only
    response = client.get("/robots.txt")
    assert response.content.decode() == "User-agent: *\nDisallow: /\n" and response["Content-Type"].startswith("text/plain")
