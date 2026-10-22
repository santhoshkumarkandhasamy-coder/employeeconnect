import 'package:employee_connect/core/utils/validators.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('password policy', () {
    test('accepts a strong password', () {
      expect(passwordIsValid('Str0ng!Passw0rd'), isTrue);
    });

    test('rejects each missing class', () {
      expect(passwordIsValid('short1!A'), isTrue); // 8 chars is allowed
      expect(passwordIsValid('Sh0rt!'), isFalse);
      expect(passwordIsValid('nouppercase1!'), isFalse);
      expect(passwordIsValid('NOLOWERCASE1!'), isFalse);
      expect(passwordIsValid('NoDigitsHere!'), isFalse);
      expect(passwordIsValid('NoSymbols123'), isFalse);
    });

    test('rejects > 128 chars', () {
      expect(passwordIsValid('Aa1!${'x' * 130}'), isFalse);
    });

    test('must not contain employee id or name token', () {
      expect(passwordIsValid('Emp001!Secret', employeeId: 'EMP001'), isFalse);
      expect(passwordIsValid('Asha!Secret1x', fullName: 'Asha Rao'), isFalse);
      expect(passwordIsValid('Blue!Secret1x', employeeId: 'EMP001', fullName: 'Asha Rao'),
          isTrue,);
    });

    test('must differ from current', () {
      expect(passwordIsValid('Str0ng!Passw0rd', current: 'Str0ng!Passw0rd'), isFalse);
      expect(passwordIsValid('Str0ng!Passw0rd', current: 'Old!Passw0rd1'), isTrue);
    });
  });

  group('employee id', () {
    test('required and trimmed', () {
      expect(validateEmployeeId('  '), isNotNull);
      expect(validateEmployeeId(null), isNotNull);
      expect(validateEmployeeId(' EMP001 '), isNull);
    });
  });
}
