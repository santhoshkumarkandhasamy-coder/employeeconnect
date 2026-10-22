import '../config/env.dart';

/// The build-time default is the placeholder and means "ask the user".
const String placeholderApiUrl = 'https://localhost/api';

bool isPlaceholderUrl(String? url) => url == null || url.isEmpty || url == placeholderApiUrl;

/// Effective base URL: saved server URL > build-time `API_BASE_URL` > null
/// (null = show Server setup).
String? resolveBaseUrl({String? saved, String buildTime = Env.apiBaseUrl}) {
  if (!isPlaceholderUrl(saved)) return saved;
  if (!isPlaceholderUrl(buildTime)) return buildTime;
  return null;
}

/// Turns what a user typed ("hr.company.com", "https://hr.company.com:8443")
/// into the API base URL ("https://hr.company.com/api"). Returns null for
/// invalid input. Release builds accept https only; no userinfo, query or
/// fragment; a trailing `/api` is added when missing.
String? normalizeServerInput(String raw, {bool release = true}) {
  var s = raw.trim();
  if (s.isEmpty || s.contains(RegExp(r'\s'))) return null;
  if (!s.contains('://')) s = 'https://$s';
  final u = Uri.tryParse(s);
  if (u == null || u.host.isEmpty) return null;
  if (u.scheme != 'https' && !(u.scheme == 'http' && !release)) return null;
  if (u.userInfo.isNotEmpty || u.hasQuery || u.hasFragment) return null;
  var path = u.path;
  while (path.endsWith('/')) {
    path = path.substring(0, path.length - 1);
  }
  if (path.isEmpty) {
    path = '/api';
  } else if (path != '/api') {
    return null;
  }
  return Uri(scheme: u.scheme, host: u.host, port: u.hasPort ? u.port : null, path: path)
      .toString();
}
