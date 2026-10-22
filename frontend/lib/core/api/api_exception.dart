import 'package:dio/dio.dart';

class FieldError {
  const FieldError(this.field, this.message);
  final String field;
  final String message;
}

/// Error mapped from the contract's error envelope (api-contract.md section 0).
class ApiException implements Exception {
  const ApiException({
    required this.code,
    required this.message,
    this.status,
    this.details = const [],
    this.requestId,
    this.retryAfter,
  });

  static const String networkCode = 'NETWORK_ERROR';
  static const String unknownCode = 'UNKNOWN';

  final String code;
  final String message;
  final int? status;
  final List<FieldError> details;
  final String? requestId;
  final Duration? retryAfter;

  bool get isNetwork => code == networkCode;
  bool get isAuthFailure =>
      code == 'UNAUTHORIZED' ||
      code == 'TOKEN_EXPIRED' ||
      isSessionRevoked ||
      code == 'ACCOUNT_INACTIVE';

  /// Codes meaning the refresh-token family is dead; refreshing cannot help.
  /// `TOKEN_REUSED` = a used token was replayed (possible theft).
  /// `SESSION_REVOKED` = ended by logout, logout-all, password change, admin
  /// reset or deactivation ("signed out elsewhere", not a theft signal).
  static const Set<String> sessionEndingCodes = {
    'TOKEN_REUSED',
    'SESSION_REVOKED',
  };
  bool get isSessionRevoked => sessionEndingCodes.contains(code);

  String? detail(String field) {
    for (final d in details) {
      if (d.field == field) return d.message;
    }
    return null;
  }

  /// For ACCOUNT_LOCKED: `details:[{field:"locked_until",message:"<ISO ts>"}]`.
  DateTime? get lockedUntil {
    final raw = detail('locked_until');
    return raw == null ? null : DateTime.tryParse(raw)?.toLocal();
  }

  factory ApiException.fromDio(DioException e) {
    final existing = e.error;
    if (existing is ApiException) return existing;

    final res = e.response;
    final data = res?.data;
    if (data is Map && data['error'] is Map) {
      final err = (data['error'] as Map).cast<String, dynamic>();
      final rawDetails = err['details'];
      final details = <FieldError>[
        if (rawDetails is List)
          for (final d in rawDetails)
            if (d is Map)
              FieldError(
                '${d['field'] ?? ''}',
                '${d['message'] ?? ''}',
              ),
      ];
      final retry = int.tryParse(res?.headers.value('retry-after') ?? '');
      return ApiException(
        code: (err['code'] as String?) ?? unknownCode,
        message: (err['message'] as String?) ?? 'Something went wrong.',
        status: res?.statusCode,
        details: details,
        requestId: err['request_id'] as String?,
        retryAfter: retry == null ? null : Duration(seconds: retry),
      );
    }

    // Non-JSON bodies (nginx HTML error pages) or JSON without the envelope.
    final fallback = _statusFallback(res);
    if (fallback != null) return fallback;

    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.connectionError:
        return const ApiException(
          code: networkCode,
          message: 'No connection. Check your internet and try again.',
        );
      case DioExceptionType.badCertificate:
        return const ApiException(
          code: 'CERTIFICATE_REJECTED',
          message: 'Could not establish a secure connection to the server.',
        );
      default:
        return ApiException(
          code: unknownCode,
          message: 'Something went wrong. Please try again.',
          status: res?.statusCode,
        );
    }
  }

  /// Status-code based mapping for proxies (nginx) that answer with plain HTML.
  static ApiException? _statusFallback(Response<dynamic>? res) {
    final status = res?.statusCode;
    if (status == null) return null;
    String code;
    String message;
    Duration? retry;
    switch (status) {
      case 413:
        code = 'PAYLOAD_TOO_LARGE';
        message = 'The file or request is too large. Choose a smaller one.';
      case 429:
        code = 'RATE_LIMITED';
        message = 'Too many requests. Please wait a moment and try again.';
        final secs = int.tryParse((res?.headers.value('retry-after') ?? '').trim());
        if (secs != null && secs >= 0) retry = Duration(seconds: secs);
      case 502:
      case 503:
      case 504:
        code = 'SERVICE_UNAVAILABLE';
        message = 'The server is temporarily unavailable. Please try again shortly.';
      default:
        return null;
    }
    return ApiException(code: code, message: message, status: status, retryAfter: retry);
  }

  @override
  String toString() => 'ApiException($code, $status, $message)';
}
