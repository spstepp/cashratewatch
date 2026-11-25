export async function onRequest(context) {
  const url = new URL(context.request.url);
  if (url.hostname === "www.cashratewatch.com") {
    return Response.redirect(
      "https://cashratewatch.com" + url.pathname + url.search,
      301
    );
  }

  // Source files are kept in the repo for the build, but must not be served.
  // A _redirects 404 is not used: Pages does not support a 404 rewrite in
  // _redirects, and _redirects is not applied to requests that hit Functions.
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const blocked = new Set(["/build.py", "/cd_seed.csv"]);
  if (blocked.has(path.toLowerCase())) {
    return new Response("Not found", {
      status: 404,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "cache-control": "no-store",
      },
    });
  }

  return context.next();
}
