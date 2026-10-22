import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/features/profile/data/profile_repository.dart';
import 'package:employee_connect/features/profile/domain/profile_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/module_fakes.dart';

final _now = DateTime(2026, 10, 21);

void main() {
  group('validators', () {
    test('phone: E.164-ish, spaces and dashes tolerated', () {
      expect(validatePhone(''), isNull);
      expect(validatePhone('+919999999999'), isNull);
      expect(validatePhone('99999 99999'), isNull);
      expect(validatePhone('+91-99999-99999'), isNull);
      expect(validatePhone('12345'), isNotNull);
      expect(validatePhone('abc12345678'), isNotNull);
      expect(validatePhone('1' * 16), isNotNull);
    });

    test('email', () {
      expect(validateEmail(''), isNull);
      expect(validateEmail('a@b.co'), isNull);
      expect(validateEmail('a@b'), isNotNull);
      expect(validateEmail('a b@c.com'), isNotNull);
    });

    test('date of birth: past, age 16 to 100', () {
      expect(validateDob('', _now), isNull);
      expect(validateDob('1995-04-12', _now), isNull);
      expect(validateDob('2026-10-21', _now), isNotNull); // today
      expect(validateDob('2012-01-01', _now), isNotNull); // 14 years
      expect(validateDob('2010-10-22', _now), isNotNull); // turns 16 tomorrow
      expect(validateDob('2010-10-21', _now), isNull); // exactly 16
      expect(validateDob('1920-01-01', _now), isNotNull); // over 100
      expect(validateDob('nope', _now), isNotNull);
    });

    test('anniversary must be in the past', () {
      expect(validateAnniversary('2020-02-01', _now), isNull);
      expect(validateAnniversary('2026-10-21', _now), isNotNull);
      expect(validateAnniversary('', _now), isNull);
    });

    test('address length', () {
      expect(validateAddress('x' * 300), isNull);
      expect(validateAddress('x' * 301), isNotNull);
    });

    test('emergency contact needs name and phone together', () {
      expect(validateEmergencyName('', ''), isNull);
      expect(validateEmergencyPhone('', ''), isNull);
      expect(validateEmergencyName('', '+919999999999'), isNotNull);
      expect(validateEmergencyPhone('Ravi', ''), isNotNull);
      expect(validateEmergencyPhone('Ravi', '123'), isNotNull);
      expect(validateEmergencyPhone('Ravi', '+919999999999'), isNull);
    });

  });

  group('Profile model', () {
    test('parses the contract object', () {
      final p = Profile.fromJson({
        'user_id': 'u1',
        'employee_id': 'EMP001',
        'full_name': 'A B',
        'department': {'id': 'd', 'name': 'IT'},
        'role': 'employee',
        'official_email': 'a@corp.com',
        'photo_url': '/api/files/abcdefgh-1',
        'dob': '1995-04-12',
        'wedding_anniversary': null,
        'phone': '+919999999999',
        'emergency_contact': {'name': 'R', 'relationship': 'Brother', 'phone': '+91888888888'},
        'blood_group': 'O+',
      });
      expect(p.department, 'IT');
      expect(p.emergency!.relationship, 'Brother');
      expect(p.weddingAnniversary, isNull);
    });

    test('tolerates missing optional parts', () {
      final p = Profile.fromJson({'employee_id': 'E1', 'full_name': 'X'});
      expect(p.emergency, isNull);
    });

    test('scrub keeps names but drops personal data from the cache', () {
      final scrubbed = scrubSensitive({
        'employee_id': 'E1',
        'full_name': 'A B',
        'department': {'id': 'd', 'name': 'IT'},
        'dob': '1995-04-12',
        'phone': '+91',
        'personal_email': 'x@y.z',
        'address': 'somewhere',
        'blood_group': 'O+',
        'emergency_contact': {'name': 'R', 'phone': '1'},
      }) as Map;
      expect(scrubbed.keys, containsAll(['employee_id', 'full_name', 'department']));
      for (final k in ['dob', 'phone', 'personal_email', 'address', 'blood_group', 'emergency_contact']) {
        expect(scrubbed.containsKey(k), isFalse, reason: k);
      }
    });
  });

  group('ProfileDraft.diff', () {
    final saved = sampleProfile(
      emergency: const EmergencyContact(name: 'Ravi', phone: '+918888888888'),
    );

    test('unchanged draft sends nothing', () {
      expect(ProfileDraft.from(saved).diff(saved), isEmpty);
      expect(ProfileDraft.from(saved).sameAs(saved), isTrue);
    });

    test('only changed keys are sent; phone is normalised', () {
      final d = ProfileDraft.from(saved).copyWith(phone: '+91 98765-43210', address: '  New address ');
      expect(d.diff(saved), {'phone': '+919876543210', 'address': 'New address'});
    });

    test('clearing a field sends an explicit null', () {
      final d = ProfileDraft.from(saved).copyWith(dob: '', bloodGroup: '');
      expect(d.diff(saved), {'dob': null, 'blood_group': null});
    });

    test('emergency contact is sent as a whole, or cleared', () {
      final changed = ProfileDraft.from(saved).copyWith(emergencyName: 'Ravi K', emergencyRelationship: 'Brother');
      expect(changed.diff(saved), {
        'emergency_contact': {'name': 'Ravi K', 'relationship': 'Brother', 'phone': '+918888888888'},
      });
      final cleared = ProfileDraft.from(saved).copyWith(emergencyName: '', emergencyPhone: '');
      expect(cleared.diff(saved), {'emergency_contact': null});
    });

  });

  group('repository', () {
    test('offline copy keeps work fields but hides personal data and disables editing', () async {
      var offline = false;
      final dio = Dio(BaseOptions(baseUrl: 'https://x.test/api'))
        ..httpClientAdapter = _ProfileAdapter(() => offline);
      final repo = ProfileRepository(dio, MemoryJsonCache(), scope: 'u1');
      final online = await repo.profile();
      expect(online.value.dob, '1995-04-12');
      expect(online.value.detailsHidden, isFalse);

      offline = true;
      final cached = await repo.profile();
      expect(cached.stale, isTrue);
      expect(cached.value.fullName, 'Asha Rao');
      expect(cached.value.department, 'IT');
      expect(cached.value.dob, isNull);
      expect(cached.value.phone, isNull);
      expect(cached.value.detailsHidden, isTrue);
    });

    test('update sends only the given keys with PUT /me/profile', () async {
      final adapter = _RecordingAdapter();
      final repo = ProfileRepository(Dio(BaseOptions(baseUrl: 'https://x.test/api'))..httpClientAdapter = adapter, MemoryJsonCache());
      await repo.update({'phone': null});
      expect(adapter.last.method, 'PUT');
      expect(adapter.last.path, '/me/profile');
      expect(adapter.last.data, {'phone': null});
    });

    test('server validation errors keep their details', () async {
      final adapter = _RecordingAdapter(
        status: 422,
        body: {
          'error': {
            'code': 'VALIDATION_ERROR',
            'message': 'bad',
            'details': [
              {'field': 'phone', 'message': 'invalid phone'},
            ],
          },
        },
      );
      final repo = ProfileRepository(Dio(BaseOptions(baseUrl: 'https://x.test/api'))..httpClientAdapter = adapter, MemoryJsonCache());
      try {
        await repo.update({'phone': '1'});
        fail('should throw');
      } on ApiException catch (e) {
        expect(profileErrorMessage(e), 'invalid phone');
      }
    });
  });
}

const _profileJson = {
  'user_id': 'u1',
  'employee_id': 'EMP001',
  'full_name': 'Asha Rao',
  'department': {'id': 'd1', 'name': 'IT'},
  'role': 'employee',
  'official_email': 'asha@corp.com',
  'dob': '1995-04-12',
  'phone': '+919999999999',
};

class _ProfileAdapter implements HttpClientAdapter {
  _ProfileAdapter(this.offline);
  final bool Function() offline;

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    if (offline()) throw DioException.connectionError(requestOptions: options, reason: 'offline');
    return ResponseBody.fromString(jsonEncode(_profileJson), 200, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType],
    },);
  }

  @override
  void close({bool force = false}) {}
}

class _RecordingAdapter implements HttpClientAdapter {
  _RecordingAdapter({this.status = 200, this.body = const {'employee_id': 'E1', 'full_name': 'X'}});
  final int status;
  final Map<String, Object?> body;
  late RequestOptions last;

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    last = options;
    return ResponseBody.fromString(jsonEncode(body), status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType],
    },);
  }

  @override
  void close({bool force = false}) {}
}
