export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    
    // ⚠️ REPLACE THIS with your actual Render URL where stream_server.py is running
    const backendUrl = "https://your-render-app-name.onrender.com";
    
    // Construct the new URL to point to Render
    const targetUrl = backendUrl + url.pathname + url.search;
    
    // Create a new request based on the original (preserves Range headers for video seeking)
    const newRequest = new Request(targetUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: "manual"
    });
    
    // Fetch the stream from Render and return it directly to the user
    const response = await fetch(newRequest);
    return response;
  }
};
