<!-- omit in toc -->
# Contributing to Alchemical Queues

First off, thanks for taking the time to contribute! ❤️

All types of contributions are encouraged and valued. See the [Table of Contents](#table-of-contents) for different ways to help and details about how this project handles them. Please make sure to read the relevant section before making your contribution. It will make it a lot easier for us maintainers and smooth out the experience for all involved. The community looks forward to your contributions. 🎉

> And if you like the project, but just don't have time to contribute, that's fine. There are other easy ways to support the project and show your appreciation, which we would also be very happy about:
> - Star the project
> - Tweet about it
> - Refer this project in your project's readme
> - Mention the project at local meetups and tell your friends/colleagues

<!-- omit in toc -->
## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [I Have a Question](#i-have-a-question)
- [I Want To Contribute](#i-want-to-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Enhancements](#suggesting-enhancements)
  - [Your First Code Contribution](#your-first-code-contribution)
    - [Format your code with `black`](#format-your-code-with-black)
    - [Rate your code style using `pylint`](#rate-your-code-style-using-pylint)
    - [Type-check your code with `mypy`](#type-check-your-code-with-mypy)
    - [Run the testsuite with `pytest`](#run-the-testsuite-with-pytest)
- [Attribution](#attribution)


## Code of Conduct

This project and everyone participating in it is governed by the
[Alchemical Queues Code of Conduct](https://github.com/thijsmie/alchemical_queues/blob/master/CODE_OF_CONDUCT.md).
By participating, you are expected to uphold this code. Please report unacceptable behavior
to <open-source@tmiedema.com>.


## I Have a Question

> If you want to ask a question, we assume that you have read the available [Documentation](https://alchemical-queues.readthedocs.io/en/latest/).

Before you ask a question, it is best to search for existing [Issues](https://github.com/thijsmie/alchemical_queues/issues) that might help you. In case you have found a suitable issue and still need clarification, you can write your question in this issue. It is also advisable to search the internet for answers first.

If you then still feel the need to ask a question and need clarification, we recommend the following:

- Open an [Issue](https://github.com/thijsmie/alchemical_queues/issues/new).
- Provide as much context as you can about what you're running into.
- Provide project and platform versions (nodejs, npm, etc), depending on what seems relevant.

We will then take care of the issue as soon as possible.


## I Want To Contribute

> ### Legal Notice <!-- omit in toc -->
> When contributing to this project, you must agree that you have authored 100% of the content, that you have the necessary rights to the content and that the content you contribute may be provided under the project license (MIT).

### Reporting Bugs

<!-- omit in toc -->
#### Before Submitting a Bug Report

A good bug report shouldn't leave others needing to chase you up for more information. Therefore, we ask you to investigate carefully, collect information and describe the issue in detail in your report. Please complete the following steps in advance to help us fix any potential bug as fast as possible.

- Make sure that you are using the latest version.
- Determine if your bug is really a bug and not an error on your side e.g. using incompatible environment components/versions (Make sure that you have read the [documentation](https://alchemical-queues.readthedocs.io/en/latest/). If you are looking for support, you might want to check [this section](#i-have-a-question)).
- To see if other users have experienced (and potentially already solved) the same issue you are having, check if there is not already a bug report existing for your bug or error in the [bug tracker](https://github.com/thijsmie/alchemical_queuesissues?q=label%3Abug).
- Also make sure to search the internet (including Stack Overflow) to see if users outside of the GitHub community have discussed the issue.
- Collect information about the bug:
  - Stack trace (Traceback)
  - OS, Platform and Version (Windows, Linux, macOS, x86, ARM)
  - Version of the interpreter, compiler, SDK, runtime environment, package manager, depending on what seems relevant.
  - Possibly your input and the output
  - Can you reliably reproduce the issue? And can you also reproduce it with older versions?

<!-- omit in toc -->
#### How Do I Submit a Good Bug Report?

> You must never report security related issues, vulnerabilities or bugs including sensitive information to the issue tracker, or elsewhere in public. Instead sensitive bugs must be sent by email to <open-source@tmiedema.com>.

We use GitHub issues to track bugs and errors. If you run into an issue with the project:

- Open an [Issue](https://github.com/thijsmie/alchemical_queues/issues/new). (Since we can't be sure at this point whether it is a bug or not, we ask you not to talk about a bug yet and not to label the issue.)
- Explain the behavior you would expect and the actual behavior.
- Please provide as much context as possible and describe the *reproduction steps* that someone else can follow to recreate the issue on their own. This usually includes your code. For good bug reports you should isolate the problem and create a reduced test case.
- Provide the information you collected in the previous section.

Once it's filed:

- The project team will label the issue accordingly.
- A team member will try to reproduce the issue with your provided steps. If there are no reproduction steps or no obvious way to reproduce the issue, the team will ask you for those steps and mark the issue as `needs-repro`. Bugs with the `needs-repro` tag will not be addressed until they are reproduced.
- If the team is able to reproduce the issue, it will be marked `needs-fix`, as well as possibly other tags (such as `critical`), and the issue will be left to be [implemented by someone](#your-first-code-contribution).


### Suggesting Enhancements

This section guides you through submitting an enhancement suggestion for Alchemical Queues, **including completely new features and minor improvements to existing functionality**. Following these guidelines will help maintainers and the community to understand your suggestion and find related suggestions.

<!-- omit in toc -->
#### Before Submitting an Enhancement

- Make sure that you are using the latest version.
- Read the [documentation](https://alchemical-queues.readthedocs.io/en/latest/) carefully and find out if the functionality is already covered, maybe by an individual configuration.
- Perform a [search](https://github.com/thijsmie/alchemical_queues/issues) to see if the enhancement has already been suggested. If it has, add a comment to the existing issue instead of opening a new one.
- Find out whether your idea fits with the scope and aims of the project. It's up to you to make a strong case to convince the project's developers of the merits of this feature. Keep in mind that we want features that will be useful to the majority of our users and not just a small subset. If you're just targeting a minority of users, consider writing an add-on/plugin library.

<!-- omit in toc -->
#### How Do I Submit a Good Enhancement Suggestion?

Enhancement suggestions are tracked as [GitHub issues](https://github.com/thijsmie/alchemical_queues/issues).

- Use a **clear and descriptive title** for the issue to identify the suggestion.
- Provide a **step-by-step description of the suggested enhancement** in as many details as possible.
- **Describe the current behavior** and **explain which behavior you expected to see instead** and why. At this point you can also tell which alternatives do not work for you.
- **Explain why this enhancement would be useful** to most Alchemical Queues users. You may also want to point out the other projects that solved it better and which could serve as inspiration.

### Your First Code Contribution

So you've decided to contribute some code to Alchemical Queues! Here a couple quick steps to get your environment up and running.

The environment used for Alchemical Queues is managed by `poetry`, a very useful tool that makes setting up the same environment every time a breeze. Install it easily via pip:

```sh
$ pip install poetry
```

Now you can clone the github repository (maybe make a fork first) and install the dependencies.

```sh
$ git clone https://github.com/thijsmie/alchemical_queues
$ cd alchemical_queues
$ poetry install
```

You can now make a change somewhere. For the sake of argument, lets just say you add a quick `print("Hi!")` somewhere in the code. Before you contribute this change back to the project you'll need to perform a couple steps:

#### Format your code with `black`

Alchemical Queues is formatted using `black`, a zero-config code formatter. Run it like so:

```sh
$ poetry run black src/
```

#### Rate your code style using `pylint`

To make sure there aren't any unused imports, non-descriptive variable names or other such "code smells" you can run `pylint`.

```sh
$ poetry run pylint src/alchemical_queues/
```

Preferably, the rating should stay '10.0', unless there is a compelling reason not to.

#### Type-check your code with `mypy`

Type hints are an important way to communicate to the user how the Alchemical Queues API works. Use `mypy` to check it:

```sh
$ poetry run mypy .
```

#### Run the testsuite with `pytest`

Testing is important, and it helps you to not accidentally break the code. We test using `pytest`. It also gives some feedback on *coverage*, which counts how many lines of your code are actually tested.

```sh
$ poetry run pytest --cov=src .
$ poetry run coverage report
```

## Attribution
This guide is based on the **contributing-gen**. [Make your own](https://github.com/bttger/contributing-gen)!
