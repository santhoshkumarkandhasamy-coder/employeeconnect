import 'package:employee_connect/features/payroll/domain/gross_split.dart';
import 'package:employee_connect/features/payroll/presentation/structure_form_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/payroll_fakes.dart';

void main() {
  group('splitGross', () {
    test('30,000 -> 15,000 / 6,000 / 9,000', () {
      final s = splitGross(30000);
      expect([s.basic, s.hra, s.allowance], [15000, 6000, 9000]);
    });

    test('odd values keep the exact sum and the remainder absorbs rounding', () {
      final s = splitGross(18333);
      expect(s.basic, 9167);
      expect(s.hra, 3667);
      expect(s.allowance, 5499);
      expect(s.total, 18333);
    });

    test('sum is exact and nothing is negative for many values', () {
      for (var g = 0; g <= 5000; g++) {
        final s = splitGross(g);
        expect(s.total, g);
        expect(s.basic >= 0 && s.hra >= 0 && s.allowance >= 0, isTrue, reason: '$g');
      }
      expect(splitGross(10000000).total, 10000000);
    });

    test('percentages are the named constants', () {
      expect(grossBasicPercent, 50);
      expect(basicHraPercent, 40);
    });
  });

  testWidgets('Split from gross fills basic, HRA and a SPECIAL row; fields stay editable', (tester) async {
    await pumpPayroll(
      tester,
      const StructureFormScreen(userId: 'u1'),
      repo: FakeRepo(),
      perms: const ['payroll.view_all', 'payroll.manage'],
    );
    await tester.enterText(find.byKey(const ValueKey('gross-target')), '18333');
    await tester.tap(find.byKey(const ValueKey('split-from-gross')));
    await tester.pumpAndSettle();

    String text(String label) =>
        tester.widget<TextFormField>(find.widgetWithText(TextFormField, label).first).controller!.text;
    expect(text('Basic (monthly)'), '9167.00');
    expect(text('HRA (monthly)'), '3667.00');
    expect(text('Code'), 'SPECIAL');
    expect(find.text('₹18,333.00'), findsOneWidget);

    // Splitting again reuses the SPECIAL row instead of adding another.
    await tester.enterText(find.byKey(const ValueKey('gross-target')), '30000');
    await tester.tap(find.byKey(const ValueKey('split-from-gross')));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(TextFormField, 'Code'), findsOneWidget);
    expect(text('Basic (monthly)'), '15000.00');
    expect(find.text('₹30,000.00'), findsOneWidget);

    // Still editable; live total follows.
    await tester.enterText(find.widgetWithText(TextFormField, 'Basic (monthly)'), '16000');
    await tester.pumpAndSettle();
    expect(find.text('₹31,000.00'), findsOneWidget);
  });
}
