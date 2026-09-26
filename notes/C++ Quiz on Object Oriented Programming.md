---
title: "C++ Quiz on Object Oriented Programming"
subject: ""
source_url: "https://docs.google.com/forms/d/e/1FAIpQLScd8fJeA8j8g82kghQN4f4_9Q-kRnUoykhotUqrYWJl-X6peg/viewform?usp=header"
date_solved: "2026-09-26T13:13:41.578514+00:00"
score: "8/10"
question_count: 11
---
# C++ Quiz on Object Oriented Programming
## Q&A
### 1. * (confidence 1.00)
**Answer:** Inheritance

### 2. * (confidence 1.00)
**Answer:** Inheritance is a feature of C++ that allows a class to acquire the properties and behaviors of another class. A base class is the existing class whose features are inherited, while a derived class is the new class that inherits those features from the base class.

### 3. A programmer wrote the following code to pass data to a parameterized base class constructor, but it produces a compilation error:

#include <iostream>
using namespace std;

class Rectangle {
    int width, height;
public:
    Rectangle(int w, int h) : width(w), height(h) {}
};

class Box : public Rectangle {
    int depth;
public:
    // Line 12: Intended constructor
    Box(int w, int h, int d) {
        Rectangle(w, h);
        depth = d;
    }
};

How should Line 12 be corrected to fix the error?
* (confidence 0.99)
**Answer:** Box(int w, int h, int d) : Rectangle(w, h), depth(d) {}

### 4. Develop a simple “Smart Calculator” module in C++ that can perform basic arithmetic operations (addition, multiplication, and finding the maximum) on different types and numbers of inputs. The user should be able to call functions with the same logical name but different arguments, and the correct version should be selected automatically by the compiler.
* (confidence 0.98)
**Answer:** #include <iostream>
#include <algorithm>
using namespace std;

class SmartCalculator {
public:
    // addition for two numbers
    template<typename T>
    T add(T a, T b) { return a + b; }

    // addition for three numbers
    template<typename T>
    T add(T a, T b, T c) { return a + b + c; }

    // multiplication for two numbers
    template<typename T>
    T multiply(T a, T b) { return a * b; }

    // multiplication for three numbers
    template<typename T>
    T multiply(T a, T b, T c) { return a * b * c; }

    // max for two numbers
    template<typename T>
    T maxVal(T a, T b) { return (a > b) ? a : b; }

    // max for three numbers
    template<typename T>
    T maxVal(T a, T b, T c) { return max(a, max(b, c)); }
};

int main() {
    SmartCalculator calc;
    cout << "Add 2 + 3 = " << calc.add(2,3) << endl;
    cout << "Add 1.5 + 2.5 + 3.0 = " << calc.add(1.5,2.5,3.0) << endl;
    cout << "Multiply 4 * 5 = " << calc.multiply(4,5) << endl;
    cout << "Max of 7 and 9 = " << calc.maxVal(7,9) << endl;
    cout << "Max of 3, 8, 5 = " << calc.maxVal(3,8,5) << endl;
    return 0;
}

### 5. Which type of inheritance occurs when multiple derived classes inherit from a single base class, forming a tree-like structure?
* (confidence 0.99)
**Answer:** Hierarchical Inheritance

### 6. When an object of a derived class is created, in what order are the constructors executed?
* (confidence 0.99)
**Answer:** Base class constructor executes first, then Derived class constructor

### 7. In what order do destructors execute compared to constructors?
* (confidence 0.99)
**Answer:** The reverse order of constructors (LIFO process)

### 8. What type of polymorphism is achieved using virtual functions in C++?
* (confidence 0.99)
**Answer:** Runtime polymorphism

### 9. Which of the following is a strict rule for virtual functions?
* (confidence 0.99)
**Answer:** Virtual functions cannot be static.

### 10. How is a pure virtual function properly declared in a base class?
* (confidence 0.99)
**Answer:** virtual void funct_name() = 0;

### 11. What is a class called if it contains at least one pure virtual function?
* (confidence 0.99)
**Answer:** An Abstract class

