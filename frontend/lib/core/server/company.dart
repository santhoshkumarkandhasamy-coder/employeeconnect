/// Company object of `GET /public/company`, `/auth/me` and `/config`
/// (api-contract.md section 12).
class CompanyInfo {
  const CompanyInfo({
    this.name,
    this.logoUrl,
    this.logoVersion = 0,
    this.configured = false,
    this.product,
  });

  final String? name;
  final String? logoUrl;
  final int logoVersion;
  final bool configured;

  /// Only set by `/public/company`; must be `employee-connect`.
  final String? product;

  bool get isEmployeeConnect => product == 'employee-connect';

  static CompanyInfo? parse(Object? j) {
    if (j is! Map) return null;
    final name = j['name'];
    final url = j['logo_url'];
    return CompanyInfo(
      name: name is String && name.trim().isNotEmpty ? name.trim() : null,
      logoUrl: url is String && url.isNotEmpty ? url : null,
      logoVersion: (j['logo_version'] as num?)?.toInt() ?? 0,
      configured: j['configured'] == true,
      product: j['product'] as String?,
    );
  }
}
