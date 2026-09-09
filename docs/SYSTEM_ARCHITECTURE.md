# LMS Core Data Architecture

## Summary

The core model is centered on people (`Student`, `Teacher`), organizations
(`School`), delivery periods (`Batch`), curriculum (`Course Level`), and learner
work (`Submission`). Schools, students, and teachers connect to batches through
child enrollment records. A teacher can link directly to a course level, but a
student cannot: the student enrollment stores a course vertical and a level
label, while a separate mapping can resolve those attributes to a `Course
Level`. A regular `Submission` stores student and assignment identifiers as
plain data, so those two relationships are application-validated rather than
database-enforced Links.

This document is derived from the current DocType JSON and the regular
onboarding/submission paths. The implementation under `tap_lms/summer_program/`
is intentionally excluded as stale.

## Relationship diagram

![LMS core data architecture](<2dced65d-3c80-474e-8c38-73d084c30634 (1).svg>)

Solid arrows are Frappe `Link` or `Table` fields. Dotted arrows are logical
relationships implemented through values and application behavior, without a
database Link on the source DocType.

## Core DocTypes

### Student

`Student` is the learner master record. Its generated identifier follows
`ST########`. The record contains profile data such as name, phone, status,
grade, language, preferences, and integration identifiers.

Relevant relationships:

- `school_id` is an optional Link to `School` and acts as the current/default
  school reference.
- `enrollment` owns zero or more `Student Enrollment` child rows. Each row must
  link to a `Batch` and can also record the `School`, grade, course vertical,
  level label, and joining date for that enrollment.
- Neither the top-level `level` nor the enrollment `level` is a Link to `Course
  Level`. They are Select values with different option sets.
- `peer_groups` is a separate collaboration child table and is outside the core
  enrollment model.

The regular onboarding path uses the latest enrollment row as the student's
active enrollment context. The latest row is selected by `date_joining`, then
child-row order. Code frequently falls back from the enrollment's `school` to
the student's top-level `school_id`.

### Teacher

`Teacher` is the educator master record, containing identity, contact,
language, role, and integration data.

Relevant relationships:

- `school_id` is an optional Link to the teacher's current/default `School`.
- `teacher_batch` is an optional direct Link to the current `Batch`.
- `course_level` is an optional direct Link to `Course Level`.
- `enrollment` owns zero or more `Teacher Enrollment` child rows. Each row must
  link to a `Batch` and can preserve its associated `School` and joining date.

The direct school and batch fields are convenient current-state references;
the child rows represent enrollment history. Regular teacher onboarding derives
`teacher_batch` from the school's latest batch enrollment and adds a matching
teacher enrollment row.

### School

`School` is the organization master. It stores name, type, geography, board,
status, operating model, points of contact, and onboarding metadata.

Relevant relationships:

- Students and teachers point to a school through their `school_id` Links.
- `poc` owns `School_POC` child rows. Each row can link a `Teacher` as a school
  point of contact; this is separate from the teacher's school enrollment.
- `batch_enrollments` owns zero or more `School Batch Enrollment` child rows.
  Each row must link to one `Batch`.
- A school can therefore participate in many batches, and a batch can serve
  many schools. `School Batch Enrollment` is the associative record.
- Each school-batch row can store a model, date of joining, and a
  `grades_courses` JSON value. The JSON maps grades to course display names; it
  is not a foreign key to `Course Level`.

The regular onboarding code treats the school enrollment with the latest `doj`
as the school's current batch configuration.

### Batch

`Batch` represents a bounded delivery period. It contains a required `Program`,
start/end dates, registration and engagement dates, active state, identifiers,
and calendar controls.

`Batch` deliberately does not own lists of schools, students, or teachers.
Those relationships are held on the other side:

- `School Batch Enrollment` links schools to batches.
- `Student Enrollment` links students to batches.
- `Teacher Enrollment` links teachers to batches.
- `Teacher.teacher_batch` provides an additional direct current-batch pointer.

A batch also has no direct Link to `Course Level`. A batch and a course level
can belong to the same `Program`, but this shared program does not enforce a
specific batch-to-course-level pairing.

### Submission

`Submission` represents learner work and the resulting processing, grading,
feedback, plagiarism, and rubric data. Its name is a generated hash.

The regular submission path establishes two logical references:

- `student_id` contains a `Student` name, but the field type is `Data` rather
  than `Link`.
- `assign_id` contains an `Assignment` name, but it is also a `Data` field.

`tap_lms.imgana.submission.submit_artwork` verifies that both referenced records
exist before inserting a submission. This gives the regular API path
application-level integrity, but it does not prevent another writer from saving
arbitrary identifiers directly to the DocType.

`Student Assignment` is a separate associative DocType with actual Links to
`Student`, `Assignment`, and `Submission`. Its schema can represent assignment
allocation and grading status, but the regular submission creation path does
not automatically create or update this record.

Other submission relationships:

- `rubric_evaluations` contains `Rubric Evaluation` child rows.
- `Plagiarism Result.submission` is an optional Link back to `Submission`.
- `Submission` has no direct school, batch, course-level, or teacher Link.
  Those contexts must be derived from the student/assignment domain if needed.
- `Teacher Submission` is a distinct DocType for teacher-uploaded course images.
  It can link directly to `Teacher` and `School`; it is not a subtype of
  `Submission`.

### Course Level

`Course Level` is a curriculum configuration for a program, course vertical,
and level/stage. It contains the course description and objectives and owns
ordered learning content through child tables.

Relevant relationships:

- `program` is a required Link to `Program`.
- `vertical` is an optional Link to `Course Verticals`.
- `stage` is an optional Link to `Stage Grades`.
- `learning_units` owns `LearningUnitList` child rows, which link learning units
  and assign week numbers.
- `learning_outcomes` owns `UnitLearningObjective` child rows.
- `Teacher.course_level` can link directly to it.
- `Grade Course Level Mapping.assigned_course_level` can select it using academic
  year, course vertical, grade, and student type.

There is no direct `Student` to `Course Level` Link in the current Student or
Student Enrollment schema. The current Student Enrollment stores `vertical` and
a level label after the legacy enrollment split. A concrete course-level record
can be resolved through `Grade Course Level Mapping` or recorded by operational
progress DocTypes such as `StudentContentLog`, `StudentQuizAttempt`, and
`StudentStageProgress`.

## Relationship catalogue

| From | Field / bridge | To | Effective cardinality | Integrity |
|---|---|---|---|---|
| Student | `school_id` | School | Many students to zero/one school | Optional Link |
| Teacher | `school_id` | School | Many teachers to zero/one school | Optional Link |
| School | `poc` → School_POC | Teacher | One school to zero/many teacher POCs | Optional Link on child row |
| Student | `enrollment` → Student Enrollment | Batch | Many-to-many over time | Required Link on child row |
| Teacher | `enrollment` → Teacher Enrollment | Batch | Many-to-many over time | Required Link on child row |
| School | `batch_enrollments` → School Batch Enrollment | Batch | Many-to-many over time | Required Link on child row |
| Teacher | `teacher_batch` | Batch | Many teachers to zero/one current batch | Optional Link |
| Teacher | `course_level` | Course Level | Many teachers to zero/one course level | Optional Link |
| Batch | `program` | Program | Many batches to one program | Required Link |
| Course Level | `program` | Program | Many course levels to one program | Required Link |
| Course Level | `vertical` | Course Verticals | Many levels to zero/one vertical | Optional Link |
| Student | grade + enrollment vertical/level | Course Level | Derived, not structurally fixed | Mapping/application logic |
| Submission | `student_id` | Student | Many submissions to one intended student | Data field; regular API validates |
| Submission | `assign_id` | Assignment | Many submissions to one intended assignment | Data field; regular API validates |
| Student Assignment | `submission` | Submission | Many allocation records can reference one submission | Optional Link |
| Teacher Submission | `teacher`, `school_id` | Teacher, School | Many submissions to zero/one teacher and school | Optional Links |

## Data ownership and consistency rules

1. Enrollment child rows belong to their parent document. Frappe supplies their
   `parent`, `parenttype`, `parentfield`, and `idx`; they are not independent
   enrollment aggregates.
2. Use enrollment rows for historical school/batch context. Treat top-level
   Student/Teacher school and batch fields as current/default pointers.
3. Do not equate a textual level label with a `Course Level` document ID.
4. Do not assume that matching `Program` Links create a batch-to-course-level
   relationship; no such foreign key exists.
5. Do not join `Submission.student_id` or `Submission.assign_id` as though the
   database guarantees them. Validate existence, or traverse `Student
   Assignment` when a populated Link record is available.
6. Do not treat `School Batch Enrollment.grades_courses` as relational data. It
   is JSON configuration interpreted by onboarding code.

## Source of truth

Primary schemas:

- [`Student`](../tap_lms/tap_lms/doctype/student/student.json)
- [`Teacher`](../tap_lms/tap_lms/doctype/teacher/teacher.json)
- [`School`](../tap_lms/tap_lms/doctype/school/school.json)
- [`Batch`](../tap_lms/tap_lms/doctype/batch/batch.json)
- [`Submission`](../tap_lms/tap_lms/doctype/submission/submission.json)
- [`Course Level`](../tap_lms/tap_lms/doctype/course_level/course_level.json)

Relationship schemas and regular application behavior:

- [`Student Enrollment`](../tap_lms/tap_lms/doctype/student_enrollment/student_enrollment.json)
- [`Teacher Enrollment`](../tap_lms/tap_lms/doctype/teacher_enrollment/teacher_enrollment.json)
- [`School Batch Enrollment`](../tap_lms/tap_lms/doctype/school_batch_enrollment/school_batch_enrollment.json)
- [`School_POC`](../tap_lms/tap_lms/doctype/school_poc/school_poc.json)
- [`Student Assignment`](../tap_lms/tap_lms/doctype/student_assignment/student_assignment.json)
- [`Grade Course Level Mapping`](../tap_lms/tap_lms/doctype/grade_course_level_mapping/grade_course_level_mapping.json)
- [`Regular student onboarding`](../tap_lms/onboarding/student_registration.py)
- [`Shared onboarding relationship helpers`](../tap_lms/onboarding/utils.py)
- [`Regular submission pipeline`](../tap_lms/imgana/submission.py)
