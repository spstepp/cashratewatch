export async function onRequest(context) {
  const url = new URL(context.request.url);
  if (url.hostname === "www.cashratewatch.com") {
    return Response.redirect(
      "https://cashratewatch.com" + url.pathname + url.search,
      301
    );
  }
  return context.next();
}
