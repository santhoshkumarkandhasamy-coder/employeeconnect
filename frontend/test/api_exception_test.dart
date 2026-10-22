import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:flutter_test/flutter_test.dart';

DioException _err(int status, Object? data, {Map<String, List<String>>? headers}) {
  final req = RequestOptions(path: '/x');
  return DioException(
    requestOptions: req,
    type: DioExceptionType.badResponse,
    response: Response<Object?>(
      requestOptions: req,
      statusCode: status,
      data: data,
      headers: Headers.fromMap(headers ?? {}),
    ),
  );
}

void main() {
  test('maps the contract error envelope', () {
    final e = ApiException.fromDio(_err(423, {
      'error': {
        'code': 'ACCOUNT_LOCKED',
        'message': 'Locked',
        'details': [
          {'field': 'locked_until', 'message': '2026-10-21T10:15:00Z'},
        ],
        'request_id': 'abc',
      },
    }),);
    expect(e.code, 'ACCOUNT_LOCKED');
    expect(e.status, 423);
    expect(e.requestId, 'abc');
    expect(e.lockedUntil, DateTime.utc(2026, 10, 21, 10, 15).toLocal());
  });

  test('reads Retry-After on RATE_LIMITED', () {
    final e = ApiException.fromDio(_err(
      429,
      {
        'error': {'code': 'RATE_LIMITED', 'message': 'Slow down'},
      },
      headers: {
        'retry-after': ['30'],
      },
    ),);
    expect(e.retryAfter, const Duration(seconds: 30));
  });

  test('connection errors become NETWORK_ERROR', () {
    final e = ApiException.fromDio(DioException(
      requestOptions: RequestOptions(path: '/x'),
      type: DioExceptionType.connectionError,
    ),);
    expect(e.isNetwork, isTrue);
  });

  test('non-envelope body becomes UNKNOWN without leaking it', () {
    final e = ApiException.fromDio(_err(500, '<html>boom</html>'));
    expect(e.code, ApiException.unknownCode);
    expect(e.message.contains('boom'), isFalse);
  });

  test('session-ending codes: SESSION_REVOKED and TOKEN_REUSED only', () {
    expect(ApiException.sessionEndingCodes, {'TOKEN_REUSED', 'SESSION_REVOKED'});
    const revoked = ApiException(code: 'SESSION_REVOKED', message: 'x', status: 401);
    expect(revoked.isSessionRevoked, isTrue);
    expect(revoked.isAuthFailure, isTrue);
    const old = ApiException(code: 'TOKEN_REVOKED', message: 'x', status: 401);
    expect(old.isSessionRevoked, isFalse);
  });

  test('CURRENT_PASSWORD_INVALID (400) is not an auth failure', () {
    const e = ApiException(code: 'CURRENT_PASSWORD_INVALID', message: 'x', status: 400);
    expect(e.isAuthFailure, isFalse);
    expect(e.isSessionRevoked, isFalse);
  });

  group('non-JSON proxy bodies', () {
    const html = '<html><body><h1>error</h1></body></html>';
    test('413 html', () {
      final e = ApiException.fromDio(_err(413, html));
      expect(e.code, 'PAYLOAD_TOO_LARGE');
      expect(e.status, 413);
      expect(e.message, contains('too large'));
    });
    test('429 html with Retry-After', () {
      final e = ApiException.fromDio(_err(429, html, headers: {'retry-after': ['30']}));
      expect(e.code, 'RATE_LIMITED');
      expect(e.retryAfter, const Duration(seconds: 30));
    });
    test('429 html without or with bad Retry-After', () {
      expect(ApiException.fromDio(_err(429, html)).retryAfter, isNull);
      expect(
        ApiException.fromDio(_err(429, html, headers: {'retry-after': ['Wed, 21 Oct']})).retryAfter,
        isNull,
      );
    });
    for (final s in [502, 503, 504]) {
      test('$s html', () {
        final e = ApiException.fromDio(_err(s, html));
        expect(e.code, 'SERVICE_UNAVAILABLE');
        expect(e.status, s);
      });
    }
    test('null and empty bodies', () {
      expect(ApiException.fromDio(_err(503, null)).code, 'SERVICE_UNAVAILABLE');
      expect(ApiException.fromDio(_err(413, '')).code, 'PAYLOAD_TOO_LARGE');
    });
    test('envelope still wins over fallback', () {
      final e = ApiException.fromDio(_err(429, {
        'error': {'code': 'RATE_LIMITED', 'message': 'Slow down'},
      }),);
      expect(e.message, 'Slow down');
    });
  });
}
